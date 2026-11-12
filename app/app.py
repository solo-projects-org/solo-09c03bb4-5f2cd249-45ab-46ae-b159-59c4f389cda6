"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障队查看页：只展示已确认入库的记录
- /backup      数据迁移备份：流式 ZIP（records.db + 已入库原照），可直接解压作新站 /data
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /photos/...  已入库的原照
"""
import os
import queue
import sqlite3
import tempfile
import threading
import uuid as uuidlib
import zipfile
from datetime import datetime, timezone

from flask import (Flask, Response, g, jsonify, render_template, request,
                   send_from_directory)
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL,          -- photos 目录下的文件名
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);
"""


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # 启动即切 WAL：备份时的长读事务不会阻塞打包期间的补传写入
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def no_store_html(resp):
    # HTML 页面不做 HTTP 缓存，保证在线时拿到最新数据；
    # 离线可用性由 Service Worker 的 Cache Storage 负责。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service Worker 必须挂在根作用域下：从 / 路径提供，控制整个站点
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/records")
def records_page():
    rows = get_db().execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows)


# ---------------- API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or \
        datetime.now(timezone.utc).isoformat()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 保存原照（文件名用 uuid，天然去重）----
    ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    fname = client_uuid + ext
    photo.save(os.path.join(PHOTO_DIR, fname))

    # ---- 入库；唯一约束兜底并发/重试竞争 ----
    try:
        cur = db.execute(
            "INSERT INTO records (uuid, lat, lng, note, photo, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_uuid, lat, lng, note, fname, created_at,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        try:
            os.remove(os.path.join(PHOTO_DIR, fname))
        except OSError:
            pass
        row = db.execute(
            "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
        ).fetchone()
        return jsonify({"ok": True, "duplicate": True,
                        "id": row["id"] if row else None})

    return jsonify({"ok": True, "duplicate": False, "id": cur.lastrowid})


@app.route("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name)


# ---------------- 备份下载（迁移到新服务器） ----------------
#
# 打包规则（见 README「数据迁移备份」）：
#   - 快照：以本请求首次读库时已提交的记录为准；打包期间旧站仍可接收补传，
#     之后新入库的记录、以及尚未入库的照片一律不进包；
#   - ZIP 解压后顶层即 records.db 与 photos/，可直接作为新站的 /data 卷内容，
#     记录 id 与 /photos/<file> 链接均保持不变；
#   - 全程流式：原照逐块写入，ZIP 与库均不整体进内存；
#   - 任何原照缺失或读写失败都直接报错，不输出残缺的包。

BACKUP_READ_CHUNK = 1024 * 1024          # 原照按 1MB 分块读取
BACKUP_QUEUE_CHUNKS = 16                 # 生产/消费之间最多缓存 16 块（约 16MB）


class BackupError(Exception):
    """预检或打包阶段的可预期失败：向网页说明原因，不提供残缺包。"""


class _Pipe:
    """有界内存管道：打包线程写，Flask 响应线程读；全程不把 ZIP 放进内存。"""

    def __init__(self, cap_chunks):
        self.q = queue.Queue(maxsize=cap_chunks)
        self.sentinel = object()
        self.closed = False
        self.cancelled = threading.Event()  # 客户端断开时置位，解除生产者阻塞

    def write(self, data):
        if data:
            data = bytes(data)
            while not self.cancelled.is_set():
                try:
                    self.q.put(data, timeout=1)
                    break
                except queue.Full:
                    continue
            if self.cancelled.is_set():
                raise OSError("客户端已中断下载")
        return len(data)

    def read(self, _size=-1):
        item = self.q.get()
        if item is self.sentinel:
            return b""
        return item

    def flush(self):
        # zipfile 关闭时会调用；流式管道无内部缓冲，空实现即可
        return None

    def close(self):
        # 幂等：worker 结束时显式调用一次，ZipFile 析构可能再调用一次
        if not self.closed:
            self.closed = True
            self.q.put(self.sentinel)


def _open_snapshot_conn():
    """打开只读连接并固定一个 WAL 快照。

    返回前执行首次读库（SELECT 开启读事务），此后该连接看到的就是此刻已提交
    的全部记录；其他连接的后续写入不影响本快照。WAL 下读不阻塞写，
    旧站在打包期间照常接收补传。
    """
    snap = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    snap.row_factory = sqlite3.Row
    snap.execute("PRAGMA query_only = ON")
    snap.execute("PRAGMA busy_timeout=5000")
    snap.execute("BEGIN")
    snap.execute("SELECT COUNT(*) FROM records").fetchone()  # 首次读库 → 快照固定
    return snap


def _build_snapshot_db(snap, tmp_path):
    """把快照中的记录逐行写入一份全新的独立库文件（不复制整库文件）。

    新库保持相同表结构与原 id，自增序列也按快照推进，新站后续入库的编号
    不会与旧记录冲突。
    """
    fresh = sqlite3.connect(tmp_path)
    try:
        fresh.executescript(SCHEMA)
        rows = snap.execute(
            "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
            "FROM records ORDER BY id ASC"
        ).fetchall()
        fresh.executemany(
            "INSERT INTO records (id, uuid, lat, lng, note, photo, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [tuple(r) for r in rows],
        )
        # 让 sqlite_sequence 与快照一致，新站 AUTOINCREMENT 继续向后编号
        max_id = snap.execute("SELECT MAX(id) FROM records").fetchone()[0]
        if max_id is not None:
            fresh.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES('records', ?)",
                (max_id,),
            )
        fresh.commit()
    finally:
        fresh.close()


def _prepare_backup():
    """预检并生成快照库。全部成功后才允许开始下载响应。

    返回 (临时库路径, [(照片相对 ZIP 内路径, 磁盘绝对路径), ...], 记录数)。
    抛出 BackupError 时不会向浏览器发送任何包内容。
    """
    if not os.path.exists(DB_PATH):
        raise BackupError("数据库文件不存在，尚无任何已入库记录可备份。")

    snap = None
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="records-snapshot-", suffix=".db",
                                        dir=DATA_DIR)
    os.close(tmp_fd)
    try:
        try:
            snap = _open_snapshot_conn()
        except sqlite3.Error as exc:
            raise BackupError(f"读取数据库失败：{exc}") from exc

        try:
            rows = snap.execute(
                "SELECT id, photo FROM records ORDER BY id ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            raise BackupError(f"读取已入库记录失败：{exc}") from exc

        # 预检全部原照：缺一张或不可读都不打包，保证不产出残缺包。
        # 不仅检查存在性，还要实际打开读一次（权限/IO 故障在响应头发送前暴露）。
        photo_items = []
        problems = []
        for r in rows:
            arcname = "photos/" + r["photo"]
            abs_path = os.path.join(PHOTO_DIR, r["photo"])
            if not os.path.isfile(abs_path):
                problems.append(f"#{r['id']}（{r['photo']}）原照缺失")
                continue
            try:
                with open(abs_path, "rb") as fp:
                    fp.read(1)
            except OSError as exc:
                problems.append(f"#{r['id']}（{r['photo']}）原照不可读：{exc}")
                continue
            photo_items.append((arcname, abs_path))
        if problems:
            raise BackupError(
                "无法生成完整备份，以下原照缺失或读取失败：" + "；".join(problems)
            )

        try:
            _build_snapshot_db(snap, tmp_path)
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(f"生成快照数据库失败：{exc}") from exc

        # 快照库必须可独立打开
        try:
            check = sqlite3.connect(tmp_path)
            got = check.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            check.close()
        except sqlite3.Error as exc:
            raise BackupError(f"快照数据库校验失败：{exc}") from exc
        if got != len(rows):
            raise BackupError("快照数据库校验失败：记录数与快照不一致。")

        return tmp_path, photo_items, len(rows)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    finally:
        if snap is not None:
            try:
                snap.rollback()  # 结束快照读事务
                snap.close()
            except sqlite3.Error:
                pass


def _backup_stream(snapshot_db_path, photo_items):
    """生成 ZIP 的后台线程 + 有界管道；逐块流式，内存占用有上限。"""
    pipe = _Pipe(BACKUP_QUEUE_CHUNKS)
    error_box = {}

    def worker():
        zf = None
        try:
            zf = zipfile.ZipFile(pipe, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
            # 顶层即 records.db，解压后可直接挂为新站 /data
            zf.write(snapshot_db_path, arcname="records.db")
            for arcname, abs_path in photo_items:
                try:
                    with open(abs_path, "rb") as fp, zf.open(arcname, "w") as zout:
                        while True:
                            chunk = fp.read(BACKUP_READ_CHUNK)
                            if not chunk:
                                break
                            zout.write(chunk)
                except OSError as exc:
                    raise BackupError(f"读取原照 {arcname} 失败：{exc}") from exc
            # 成功才写 ZIP 中央目录
            zf.close()
            zf = None
        except Exception as exc:  # 含 BackupError / OSError / zipfile 错误
            error_box["error"] = exc
            # 故意不调用 zf.close()：否则会写出中央目录，拼出一个能被解压工具
            # 打开的"半完整"包。置 None 后由 ZipFile 析构仅释放底层文件对象。
            zf = None
        finally:
            try:
                os.remove(snapshot_db_path)
            except OSError:
                pass
            pipe.close()

    threading.Thread(target=worker, name="backup-zip", daemon=True).start()

    def generate():
        try:
            while True:
                chunk = pipe.read()
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            # 客户端中途断开：通知生产者解除阻塞并清理临时文件
            pipe.cancelled.set()
            raise
        err = error_box.get("error")
        if err is not None:
            # 响应头已发出，无法再改状态码；中断流并报错，
            # 而不是交付一个看起来成功的残缺包（该 ZIP 无中央目录，解压必失败）。
            raise OSError(f"备份打包中断：{err}")

    return generate()


@app.route("/backup")
def backup_download():
    try:
        snapshot_db_path, photo_items, record_count = _prepare_backup()
    except BackupError as exc:
        return render_template("backup_error.html", reason=str(exc)), 500

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    resp = Response(
        _backup_stream(snapshot_db_path, photo_items),
        mimetype="application/zip",
        headers={
            "Content-Disposition":
                f'attachment; filename="patrol-backup-{stamp}.zip"',
            # 备份内容必须实时生成，不允许 Service Worker / 浏览器缓存
            "Cache-Control": "no-store",
        },
    )
    resp.headers["X-Backup-Records"] = str(record_count)
    return resp


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
