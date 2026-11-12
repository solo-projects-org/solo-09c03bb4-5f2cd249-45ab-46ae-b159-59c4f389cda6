# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite，供清障队在网页上查看并安排清障。

## 功能与实现

| 需求 | 实现 |
| --- | --- |
| 首次在线打开后，断网刷新也能填写 | Service Worker 安装时缓存应用外壳（页面/JS/CSS/样例照片），断网时导航请求回退到缓存 |
| 待传记录不丢 | 记录（含照片 Blob）存入浏览器 IndexedDB，刷新、关机重启均在 |
| 回网点补传，坐标+原照入 SQLite | `POST /api/records`（multipart），照片落盘、元数据入库 |
| 收到确认才移出待传 | 浏览器仅在收到服务器 `{"ok": true}` 响应后才删除 IndexedDB 中的该笔 |
| 确认丢失，重传只入库一次 | 每笔记录保存时生成 UUID 作幂等键；`records.uuid` 唯一约束，重复提交返回 `duplicate: true` 直接确认，不重复写行、不重复存照片 |
| 入库后才供清障队查看 | `/records` 页面只渲染 SQLite 中已确认的记录 |
| 随附样例照片 | `app/static/sample-photo.png`（演示时无相机可点"使用样例照片"，离线也能取到） |
| 换服务器迁移数据 | 清障页 `/records` 顶部「下载备份 ZIP」（`GET /backup`），见下方「数据迁移备份」 |

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。

SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。

## 演示：断网采集 → 回网补传

1. **首次在线打开** `http://localhost:8000`，页面右下角无提示即已就绪（Service Worker 已缓存全部外壳资源）。
2. **模拟断网**（任选其一）：
   - 浏览器 DevTools → Network → 勾选 **Offline**；或
   - `docker compose stop`（更真实地模拟"林道上没信号"）。
3. **刷新页面** —— 页面照常打开。填写经纬度（或点"获取当前定位"），选择照片
   （演示可点"使用随附样例照片"），点 **保存到待传记录**。可多采几笔，再次刷新待传列表仍在。
4. **恢复网络**：取消 Offline，或 `docker compose start`。
5. 点 **📤 补传全部待传记录** —— 每笔收到服务器确认后才从待传列表移除。
6. 打开 **/records**（清障队查看页）即可看到已入库的坐标与原照。

**验证幂等**：补传途中若在服务器写入后断网（确认丢失），该笔仍留在待传；
再次补传时服务器按 UUID 判重，返回 `duplicate: true`，浏览器正常移出待传，
SQLite 中始终只有一行。

## 数据迁移备份（换服务器）

林场换服务器时，数据员在**清障队查看页 `/records` 顶部**点「⬇️ 下载备份 ZIP」。

**包内容与语义**

- ZIP 解压后顶层就是 `records.db` 与 `photos/`，可直接作为新站容器的 `/data` 卷内容；
- `records.db` 是一份可独立打开的 SQLite 库，表结构、记录 `id`（编号）与
  `/photos/<文件名>` 链接全部保持不变，新站打开清障页即可查看原照；
- **快照语义**：备份以本次请求首次读库时**已提交入库**的记录为准。旧站在打包
  期间仍可正常接收补传（WAL 下读不阻塞写），但打包开始后新入库的记录、以及
  尚未入库（还在浏览器待传队列里）的照片都不会进本次包；
- **全程流式，不整包进内存**：原照按 1MB 分块读取，经有界队列（最多缓存 16 块）
  流式写入 ZIP；临时快照库落盘在 `/data` 并在结束后清理，数据库也不整体读入内存；
- **不出残缺包**：预检阶段逐张核对库中引用的原照（既检查存在性，也实际打开
  验证可读），任何一张缺失、不可读，或数据库/磁盘读写失败，都在发送下载响应前
  返回说明具体原因的错误页（HTTP 500），不会发送半成品 ZIP；流式阶段若出错则
  该 ZIP 不写中央目录（解压必失败），绝不伪装成成功下载。

**在新服务器恢复**

```bash
# 1. 解压备份，得到 restored-data/records.db 与 restored-data/photos/
mkdir -p restored-data && unzip patrol-backup-*.zip -d restored-data

# 2. 用恢复覆盖文件把该目录绑定挂载为新站 /data
docker compose -f docker-compose.yml -f docker-compose.restore.yml up --build
```

打开新站 `http://<新服务器>:8000/records`，原编号与原照链接均可正常打开；
此后新补传的记录编号在旧最大值之后继续递增（快照库已带 `sqlite_sequence`）。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data python3 app/app.py        # 或：gunicorn --chdir app -b 0.0.0.0:8000 app:app
```

## 目录结构

```
├── docker-compose.yml         # 一键启动，命名卷持久化 /data
├── docker-compose.restore.yml # 新服务器恢复：绑定挂载解压后的备份目录
├── Dockerfile                 # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py                 # Flask 路由 + SQLite（uuid 唯一约束幂等 + 流式备份）
│   ├── templates/
│   │   ├── index.html         # 采集页（巡护员）
│   │   ├── records.html       # 已入库记录页（清障队，含备份下载入口）
│   │   └── backup_error.html  # 备份失败原因页（不提供残缺包）
│   └── static/
│       ├── app.js             # IndexedDB 待传队列 + 补传逻辑
│       ├── sw.js              # Service Worker（离线外壳缓存，不拦截 /backup）
│       ├── style.css
│       └── sample-photo.png   # 随附样例照片
└── tools/
    └── make_sample_photo.py   # 样例照片生成脚本（纯标准库）
```

## API

- `GET  /api/records` — 已入库记录 JSON 列表
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `GET  /photos/<file>` — 已入库原照
- `GET  /backup` — 数据迁移备份 ZIP（流式）
  - 成功：`200 application/zip`，附件名 `patrol-backup-<UTC时间戳>.zip`，
    响应头 `X-Backup-Records` 为本快照记录数
  - 原照缺失/读写失败：`500 text/html` 错误页，说明具体原因，不返回残缺包
