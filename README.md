# WeChat Vault · 微信数据方舟

> 把微信聊天记录长期归档，随时在浏览器里像用微信一样翻看、全文检索。
> 开源、自托管、多账号零成本。

[![GitHub](https://img.shields.io/badge/GitHub-youjin--a--lihua%2Fwechat--vault-07c160)](https://github.com/youjin-a-lihua/wechat-vault)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 这是什么

一个自托管的微信聊天记录**归档 + 查看**系统，对标付费闭源的「微备份（WxBackup）」。

- **双轨制** —— 既存原始备份包（灾难恢复），也存可读记录（随时查看检索）
- **全类型附件** —— 图片（含 WxAM 原图）、视频、语音、文件、表情、头像
- **全文检索** —— 中文三元组分词索引，全局搜索 + 会话内搜索 + 按月历查找
- **占用低** —— 空闲零常驻，查看时约 150–250 MB
- **永久可读** —— 数据是标准 SQLite + 文件，不依赖本工具存活
- **自动同步** —— 微信一有新消息就自动入库，页面自动刷新

### 两条轨各干什么

| 轨道 | 内容 | 解决什么 |
|---|---|---|
| **可读轨** | 解密/解码后的聊天记录（文字 + 图片/视频/语音） | 长期查看、检索、统计 |
| **存档轨** | 微信备份包（`.bak` / `BAK_*`）+ `xwechat_files` 全量 | 灾难恢复（手机丢了能还原） |

### 数据流

```
微信客户端（Linux 版 / Windows 版）
        │  xwechat_files/<wxid>/db_storage/message_*.db  ← SQLCipher 4 加密
        ▼
  ① wv_sync.py story        解密 → 入库 → 去重        → vault.db
  ② wv_media.py all         解密图片/视频/语音 → 媒体   → media/
  ③ wv_raw.py               存档原始包（冷备）          → raw-snapshots/
        ▼
  viewer/wv_server.py       FastAPI + 原生前端           → 浏览器
```

---

## ⚠️ 关于合规，如实说明

本项目包含**对微信本地加密文件的离线处理**（`.dat` 解密、WxAM 解码、密钥派生）。

- 这些能力仅用于**你自己的、合法的个人数据归档**。
- 请遵守当地法律与《微信用户协议》。
- 同类开源项目（chatlog、cloudbak、wechat-decrypt 等）曾收到腾讯下架函件；
  本项目公开发布，**使用与传播的风险由使用者自行判断和承担**。
- **本项目不内置、不代跑、不提供下载指向、不特指任何第三方取钥工具。**
  密钥获取请走下文「首次使用：密钥从哪来」的合规路线。

> 补充说明：微信自带的「聊天记录管理 → 导入与导出」**无法导出可用的明文数据**
> （尤其是附件原图）。这正是本项目必须做离线解密的根本原因——
> 「完全合规、零解密」的官方通道，实际上拿不到完整的聊天记录（含附件）。

---

## 快速开始（Docker，推荐）

```bash
git clone https://github.com/youjin-a-lihua/wechat-vault.git
cd wechat-vault/deploy
cp ../env.example .env
# 填好 .env：WV_BIND_IP（NAS 内网 IP）、WV_PASSWORD（桌面口令）
sudo docker compose -f docker-compose.single.yml up -d
```

打开 `http://<NAS_IP>:8790`。容器内嵌官方微信 Linux 客户端 + KasmVNC 网页桌面，
扫码登录后即可把客户端里的聊天记录同步进方舟。

> **首次安装向导**：第一次打开页面会进入账户配置向导，
> 设置访问密码后即可使用（也可跳过，仅靠网段白名单保护）。

### 部署后检查清单

| 检查项 | 命令 | 期望 |
|---|---|---|
| 容器健康 | `docker ps` | `healthy` |
| 服务可用 | `curl http://<NAS_IP>:8790/healthz` | `{"ok":true,"db":"ok"}` |
| 数据已载入 | `curl http://<NAS_IP>:8790/api/status` | `conv_count` > 0 |
| 日志正常 | `docker logs wechat-vault --tail 30` | 无 Traceback |

---

## 从微信客户端同步

微信登录后，聊天记录存在本地加密数据库（SQLCipher 4）里。

**默认全自动**：容器内常驻 `svc-autosync` 服务，监视微信消息库的
`(大小, mtime)` 指纹。一有新消息落盘，自动完成
「解密 → 入库 → 媒体关联 → 查看器重载」，打开网页即可看到最新内容，无需手动操作。

- 默认轮询 60s，检测到变化后再等 30s 确认写入完成（避免读到写了一半的库）
- 状态存 `/data/logs/autosync.state`，重启后不重复跑已同步的数据
- 日志：`/data/logs/autosync.log`
- `WV_AUTOSYNC=0` 可关闭；`WV_AUTOSYNC_POLL` / `WV_AUTOSYNC_STABLE` 调整节奏

**页面也是自动更新的**：打开方舟页面后不用管，它每 20 秒查一次后端状态，
有新消息会直接追加到当前会话底部（不会打断你正在翻看的历史）。
端到端延迟约 1.5~3 分钟（微信落盘 + 同步 + 页面轮询叠加），嫌慢可把
`WV_AUTOSYNC_POLL` 调到 20、`WV_AUTOSYNC_STABLE` 调到 10。
想彻底关掉页面自动刷新：浏览器控制台执行 `localStorage.wv_autorefresh='0'` 再刷新。

**非中国时区必须设 `WV_TZ_OFFSET`**（见 `env.example`），否则消息时间会整体偏移。

**手动同步**（排查问题或立即触发）：

```bash
docker exec wechat-vault /opt/wx-venv/bin/python3 /app/archiver/wv_sync.py story \
    --wechat-files /config/xwechat_files \
    --keys /data/wx_backup/all_keys.json \
    --vault /data/vault.db \
    --plain-out /data/wx_backup/plain_dbs
```

`story` 做三件事：解密数据库 → 读消息写入 vault.db → 供查看器展示。
之后跑一次媒体链路把图片、视频、语音接上（见下文「核心能力」）。

也可以直接触发 HTTP 接口（等价于上面两步）：

```bash
curl -X POST http://<NAS_IP>:8790/api/ingest
```

---

## 首次使用：密钥从哪来

方舟读的是微信自己的 SQLCipher 加密库，**必须先拿到密钥才能解密**。
取钥这一步没有长期稳定的「一键」——微信把密钥放在进程内存里、且存放方式随版本变，
但**取到之后可以用很久**：密钥在大版本内通用（换钥只发生在 3.x→4.0 这种级别），
所以正确的目标是「**取一次、长期续用**」，而不是每次都一键。

配套插件 [`plugins/wv-keymate/`](plugins/wv-keymate/) 把「取到密钥之后」的
十分钟变成确定性体验：校验 → 规范化 → 投递 NAS → 触发同步。下面三步走完即可。

### 第一步：取（三选一）

| 路线 | 做法 | 适用 |
|------|------|------|
| **① 降级取钥**（首选） | 临时装旧版微信（**≤ 4.0.3.36**）→ 登录 → 跑 `extract_scan.py` 捕获窗口 → 拿到 passphrase 后升回新版 | 愿意临时降级一次。零注入、零账号风险 |
| **② 自备工具取** | 社区存在各类取钥工具，自行取得密钥材料（64 hex passphrase 或 `all_keys.json`）后交第二步 | 不便降级、已有取钥途径 |
| **③ 完全不碰密钥** | 用第三方工具在 Windows 上导出聊天记录成 JSON，再走下文「搬过来」的方案 B 导入 | 只要一次性快照。代价：接不上自动同步 |

> **为什么首选降级路线**：微信 **4.0.3.39（Win）/ 4.0.3.88（Mac）起 dbkey 用完即从内存
> 释放**，新版本的瞬时扫描很容易扑空；旧版（≤4.0.3.36）密钥常驻内存，扫描稳定。
> 而密钥大版本内通用，降级取一次、升回新版照样用。
>
> **Docker 容器内取不了密钥**——密钥只在 Windows 宿主机侧可见。取钥必须在 Windows
> 上做，产物投递到 NAS 后容器自动续用。
>
> **路线②的风险告知**：注入类取钥工具有理论上的账号风控风险（社区无定论），
> 且这类工具受 DMCA 反规避条款约束（2025-2026 腾讯已多轮清理同类开源项目）。
> 本项目**不内置、不代跑、不提供下载指向、不特指任何工具**，法律与账号风险自担。
> 本插件只用文档化的 Windows 调试 API（`MiniDumpWriteDump` 只读快照）取内存，
> **不注入、不改动微信进程**。

### 第二步：交（校验 + 规范化 + 投递）

```powershell
# 64 hex passphrase 直接粘贴，或拖入 JSON 文件（两种旧格式自动兼容）：
python plugins\wv-keymate\keymate.py run <64位hex串或json文件> --db-dir <db_storage> --nas <NAS私网IP>
```

`run` 一次跑完：导入 → **解密裁判校验**（按 SQLCipher 4 页模型真解一遍、验 SQLite 头，
不是猜 HMAC）→ 写 `passphrase.json`（含元数据）+ `all_keys.json` → robocopy 到
`\\<NAS>\wechat-vault\wx_backup\` → `POST /api/ingest` 触发同步。
也可以拆开跑 `import / verify / deliver / trigger`；一键投递用 `drop.ps1`。

### 第三步：用（自动续用）

NAS 端 `archiver/wv_sync.py` 优先读 `passphrase.json`（账号级，**推荐**：库被微信
重建也能重新派生），回退 `all_keys.json`（逐库 `enc_key` 快照，库重建后失效）。
之后自动续跑同步，直到下次大版本换钥——换钥后回第一步重取一次即可。

> **实测提示**：24 个库的实测中，`passphrase.json` 派生 24/24 全部命中；
> 而 `all_keys.json` 快照有 1 个库（`message_resource.db`）因被微信重建、salt 变了而失效。
> **所以优先用 passphrase.json**。
>
> 容器内 `tools/` 下另有三个探测工具（`wv_keymem` / `wv_keyscan` / `wv_winkey`），
> 供逆向调试用，不承诺开箱即用——微信的密钥存放位置与版本强相关，换版本可能要重新调试。
> 详情见 [`plugins/wv-keymate/README.md`](plugins/wv-keymate/README.md)。

---

## 把其他电脑上的聊天记录搬过来

上面讲的是「本机（容器内）微信登录后自动同步」。如果你的完整历史在**另一台电脑**上，
方舟默认看不到它——它只读容器内 `/config/xwechat_files`。

三条路，按可控程度排序。

### 方案 A：拷数据目录，单独导入（推荐）

**适用**：能拿到原电脑的微信数据目录和密钥文件。
**好处**：完全不碰容器内微信的数据目录，不影响它的登录态，导入完就是一次性的事。

**① 原电脑上彻底退出微信**（托盘右键退出）。这一步别省——库还在写的时候拷出来可能是半截。

**② 把两样东西拷到 NAS**（SMB、scp、移动硬盘都行）：

| 拷什么 | 原机器位置 | 拷到 NAS |
|---|---|---|
| 微信数据目录 | `xwechat_files/<wxid>_<设备后缀>/` | `/data/imported/old-pc/xwechat_files/` |
| 密钥（两个都要） | `all_keys.json` + `passphrase.json` | `/data/imported/old-pc/keys/` |

目录长这样就行：

```
/data/imported/old-pc/
├── xwechat_files/
│   └── wxid_xxxxxxxx_2c37/        ← 整个账号目录，含 db_storage/ 和 msg/
└── keys/
    ├── all_keys.json
    └── passphrase.json
```

**③ 导入消息**：

```bash
docker exec wechat-vault /opt/wx-venv/bin/python3 /app/archiver/wv_sync.py story \
    --wechat-files /data/imported/old-pc/xwechat_files \
    --keys        /data/imported/old-pc/keys/all_keys.json \
    --vault       /data/vault.db \
    --plain-out   /data/wx_backup/plain_dbs
```

**④ 接上图片视频**（不做这步，消息都在但图全是「[图片]」占位）：

```bash
docker exec wechat-vault /opt/wx-venv/bin/python3 /app/archiver/wv_media.py all \
    --attach       /data/imported/old-pc/xwechat_files/wxid_xxxxxxxx_2c37/msg/attach \
    --account-root /data/imported/old-pc/xwechat_files/wxid_xxxxxxxx_2c37 \
    --vault        /data/vault.db \
    --plain-dbs    /data/wx_backup/plain_dbs \
    --out          /data/media
```

最后打开网页刷新即可看到。导入完成后 `/data/imported/` 可以删掉，
之后的新消息继续由容器内微信的自动同步负责。

> **会话归并规则**：`conv_id` 以 **username**（`wxid_*` / `*@chatroom`）为键，
> 显示名（备注、群名）只是 `conv.title`。所以改备注**不会**分裂会话、
> **不会**重插历史。跨机器导入时同一会话自动合到一起。
> 历史遗留的「显示名同名多人」脏数据会被标记为 legacy/stuck 保留原键。

**可能卡住的地方**：

- **报「缺少列 xxx，可能不兼容」**：两台机器的微信大版本不一致（库结构变了）。
  升级原机器的微信到与容器内相同的大版本再拷，或改走方案 B。
- **报「解密完成 0/24」**：密钥没能跨设备用。微信的 `passphrase.json` 通常
  是账号级的，但不保证所有版本都如此——这种情况改走方案 B。
- **数据量大**：`xwechat_files` 几十 GB 是常态，图片视频占大头。
  先只拷 `db_storage/` 也能导入文字消息，附件以后补。

### 方案 B：导出成文件再导入

**适用**：拿不到密钥，或者两台机器微信版本对不上。
**代价**：依赖第三方导出工具，且导出的是「当时那一刻」的快照。

1. 在原电脑上用导出工具把聊天记录导成 JSON / HTML / CSV / TXT
2. 文件丢进容器的 `/data/inbox/<随便起的账号名>/`
3. 网页上点「立即解析」，或者：

```bash
docker exec wechat-vault /opt/wx-venv/bin/python3 /app/archiver/wv_archiver.py scan \
    --source /data/inbox --store /data
```

字段名会自动嗅探，兼容 WeChatMsg / PyWxDump / 微信官方导出这几种常见格式。

### 方案 C：用微信自带的「聊天记录迁移」

源电脑微信里有「设置 → 聊天 → 聊天记录迁移与备份」，可以把记录迁到容器里的微信。
好处是走官方通道、不碰密钥；限制是**微信对同一账号多台电脑同时在线的规则
会决定这条路顺不顺**，迁移期间可能要在两台机器之间反复登录切换。

适合只想搬一部分会话的场景。这条路的细节由微信客户端决定，本项目不做干预——
迁完之后容器内的微信有了数据，自动同步会照常接手。

---

## 核心能力

### 归档与解密

| 能力 | 说明 |
|---|---|
| `.dat` V2 解密 | 账号级密钥**离线派生**（无需运行微信、无需扫内存），批量解密图片 |
| WxAM / wxgf 原图 | 微信私有压缩格式，内层是**标准 HEVC**，用 ffmpeg 还原全分辨率 |
| 视频 / 语音 / 文件 / 表情 / 头像 | 分别按实测判据关联（见 `archiver/wv_media.py` 顶部注释） |
| 增量幂等 | 重复运行自动去重，不会产生重复数据 |
| 媒体文件索引 | `_index.json` 原子写入，进程中断不会留下半截 JSON |

**关于 `.dat` 解密**：微信 4.x（2025-08+）的 `.dat` 是「15 字节头 + AES-128-ECB + 16 字节固定块 + 单字节 XOR 尾」。
密钥由 `code`（暴力搜索，约束 `code & 0xFF == xor_key`）与 `wxid` 派生。
实测：单账号 15,436 个 wxgf 原图全部解码，消息↔图片关联命中率 98%+。

### 查看器

| 功能 | 说明 |
|---|---|
| 微信式聊天界面 | 左右气泡、时间分隔、系统消息，Apple 风格双主题 |
| 会话列表 | 头像、时间跨度、消息条数，按最近/条数/名称排序 |
| 全文检索 | 全局（`Ctrl+K`）+ 会话内（`Ctrl+F`）+ 按月历查找 |
| 原图查看 | 列表用缩略图，点开加载 WxAM 全分辨率原图 |
| 统计报告 | 消息总数、收发比例、类型分布、活跃时段、发言排行 |
| 导出 | 单会话 / 按账号 / 全库，HTML · CSV · TXT · JSON |
| 移动端 / PWA | 响应式，可安装到桌面，聊天数据不缓存 |
| 长会话优化 | DOM 上限 3000 条自动回收，18 万条消息的会话也能流畅翻 |

### 媒体链路（手动跑）

```bash
# ① 定标账号级密钥（离线派生，结果缓存到 <媒体目录>/_keys.json）
python archiver/wv_media.py keys --attach <账号>/msg/attach --out <媒体目录>

# ② 批量解密图片
python archiver/wv_media.py decode --attach <账号>/msg/attach --out <媒体目录>

# ③ 关联消息 ↔ 图片
python archiver/wv_media.py link --vault <vault.db> --plain-dbs <明文库> --out <媒体目录>

# ④ 解码 WxAM 原图（wxgf → 全分辨率 JPEG）
python archiver/wv_media.py wxam --attach <账号>/msg/attach \
    --account-root <账号根> --vault <vault.db> --out <媒体目录> --plain-dbs <明文库>

# 一条龙（等价于 ①→④）
python archiver/wv_media.py all --attach <账号>/msg/attach --account-root <账号根> \
    --vault <vault.db> --plain-dbs <明文库> --out <媒体目录>
```

---

## 部署

### 单容器一体化（推荐）

复用 [WechatOnCloud（WOC）](https://github.com/leozc/WeChatOnCloud) 的架构：容器内跑
Xvfb + 微信官方 Linux 版 + KasmVNC 网页串流。首次启动从腾讯 CDN 下载微信本体（约 200MB）。

```bash
cd deploy
sudo docker compose -f docker-compose.single.yml up -d
```

1. 打开 `http://<NAS_IP>:8790`（面板）
2. 「微信运行时」卡片显示下载/安装进度
3. 装好后打开微信桌面（KasmVNC，默认映射到 13000），扫码登录
4. 登录后自动同步会接手（无需手动跑 `wv_sync.py`）

> 也可以走「手机微信 → 聊天记录迁移与备份 → 备份到电脑」把历史记录迁进客户端，
> 之后自动同步照常接手。

### 红线

- **绝不开公网**：端口只绑内网 IP（`WV_BIND_IP`），默认网段白名单
- 不引入 `docker.sock`
- 空闲时 `docker stop` 即可，不占日常登录位
- 密钥文件只落本地/内网，`local_keys/` 与 `keymate_out/` 已被 `.gitignore` 挡住

---

## 环境变量（`deploy/.env`，参考 `env.example`）

### 必填

| 变量 | 说明 |
|---|---|
| `WV_BIND_IP` | 绑定内网 IP（缺失即启动报错） |
| `WV_PASSWORD` | KasmVNC 桌面口令 |
| `WV_STORE` | 归档数据目录（vault.db、media/、wx_backup/ 都在它下面），**必须绝对路径** |

### 鉴权与网络

| 变量 | 说明 |
|---|---|
| `WV_PASSWORD_HASH` / `WV_SECRET` | 查看器访问密码 / 会话签名密钥（可选） |
| `WV_ALLOW_CIDRS` | 允许访问的网段白名单，支持 IPv6（如 `192.168.0.0/16,fd00::/8`） |
| `WV_TRUST_PROXY` | 置 `1` 才信任 `X-Forwarded-For` / `X-Real-IP`。**直连部署必须留空** |
| `WV_COOKIE_SECURE` | 置 `1` 强制会话 Cookie 带 `Secure`；走 HTTPS 反代时用 |
| `WV_TOKEN_TTL_DAYS` | 会话有效期（天），默认 7，下限 1 小时；剩余不足 24h 自动续签 |
| `WV_AUTH_FILE` | 访问控制状态文件路径（默认 `/data/auth.json`） |

### 同步与媒体

| 变量 | 说明 |
|---|---|
| `WV_AUTOSYNC` | `0` 关闭自动同步（默认开） |
| `WV_AUTOSYNC_POLL` | 轮询间隔秒，默认 60 |
| `WV_AUTOSYNC_STABLE` | 写入稳定判定秒，默认 30 |
| `WV_TZ_OFFSET` | 时区偏移（默认 8；非中国时区**必须改**） |
| `WV_WECHAT_FILES` | 微信数据目录（容器内默认 `/config/xwechat_files`） |
| `WV_KEYS_JSON` | 密钥文件路径（默认 `/data/wx_backup/all_keys.json`） |
| `WV_PLAIN_DBS` | 解密明文库目录 |
| `WV_MEDIA` | 媒体输出目录 |
| `WV_MEDIA_AUTO` | 自动跑媒体链路（默认开） |
| `WV_FFMPEG` / `WV_FFMPEG_TIMEOUT` | ffmpeg 路径 / 超时秒 |
| `WV_SQLCIPHER` | sqlcipher 可执行（纯 Python 解密时不需） |

### 归档与冷备

| 变量 | 说明 |
|---|---|
| `WV_ARCHIVE_RAW` | 存档原始包（默认开） |
| `WV_COLD` / `WV_COLD_DIR` | 冷备盘路径（留空跳过） |
| `WV_SYNC_COLD` | 同步时写冷备（默认开） |
| `WV_RAW_INBOX` / `WV_RAW_SNAPSHOTS` | 原始包收件/快照目录 |
| `WV_SCAN_HOUR` | 每日全量扫描的小时（3 = 凌晨 3 点） |
| `WV_AUTO_SCAN_MIN` | 即时解析轮询分钟（0 = 关闭） |
| `WV_INBOX` | 导入收件目录（方案 B 用） |

### 服务与日志

| 变量 | 说明 |
|---|---|
| `WV_PORT` / `WV_KASMVNC_PORT` | 面板端口 / 桌面端口（默认 8790 / 13000） |
| `WV_LOG_FILE` / `WV_LOG_MAX_MB` / `WV_LOG_KEEP` | 日志落盘路径 / 单文件上限 MB / 保留份数 |
| `WV_LOG_LEVEL` | 日志级别 |
| `WV_LOGS` / `WV_DATA_DIR` / `WV_ROOT` | 日志/数据/根目录 |

> ⚠️ `.env` 里的 `$` 必须写成 `$$`（docker compose 会做变量插值）。
>
> ⚠️ `WV_TRUST_PROXY` 只在前面**确实**有反向代理时才能打开。直连暴露时
> 这个头是客户端随便写的，打开等于把网段白名单和「仅本机可 reload」
> 一起送人——伪造一行 `X-Forwarded-For: 127.0.0.1` 就能进。
>
> ⚠️ 会话失效的三种方式：单点登出（吊销该 `sid`）、
> `POST /api/logout {"all":true}`（递增 epoch，踢掉所有设备）、
> 或改 `WV_SECRET`（全部作废）。

---

## HTTP API

### 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页 |
| POST | `/api/login` | 登录（获取会话 Cookie） |
| POST | `/api/logout` | 登出（`{"all":true}` 踢全部设备） |
| GET | `/api/auth` | 查询认证状态 |
| POST | `/api/setup` / `/api/setup/skip` | 首次安装向导 |

### 数据

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 库状态（会话数/消息数/加载状态） |
| GET | `/api/accounts` | 账号列表 |
| GET | `/api/conversations` | 会话列表 |
| GET | `/api/messages` | 会话内消息（分页） |
| GET | `/api/search` | 全文检索（关键词 + 类型 + 日期区间） |
| GET | `/api/calendar` | 按月历查找（每月消息数） |
| GET | `/api/locate` | 消息定位（跳转到指定消息） |
| GET | `/api/stats` | 统计报告 |

### 同步与归档

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/reload` | 重载数据库（仅本机可调） |
| POST | `/api/ingest` | 立即归档 + 重载 |
| GET | `/api/archive` | 归档任务状态 |
| GET | `/api/wechat/status` | 微信运行时状态 |
| POST | `/api/wechat/install` | 安装微信运行时 |

### 导出与静态资源

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/export/conv` | 导出单会话（HTML/CSV/TXT/JSON） |
| GET | `/api/export/all` | 导出全库 |
| GET | `/api/export/manifest` | 导出清单 |
| GET | `/media/{path}` | 解码后的媒体（含路径穿越防护） |
| GET | `/wechat-desktop` | KasmVNC 桌面入口 |
| GET | `/`, `/app.js`, `/app.css`, `/sw.js`, `/manifest.webmanifest`, `/icon.svg` | 前端资源 |

---

## 排障

| 症状 | 原因 | 处理 |
|---|---|---|
| 打开页面 503「数据尚未加载」 | 启动方式错了（用了 `uvicorn viewer.wv_server:app`） | 必须用 CLI 模式：`wv_server.py --store /data` |
| 解密完成 0/24 | 密钥失效或跨设备不通用 | 重取密钥（见「密钥从哪来」）；优先用 `passphrase.json` |
| `session.db` 解密失败 | 会话归属无法确定，同步会静默停止 | 同上，重取账号级密钥 |
| 搜索两字词无结果 | trigram 分词器需 ≥3 字符 | 代码已自动回落 LIKE，短词请用 3 字以上关键词 |
| 图片全是「[图片]」占位 | 媒体链路没跑 | 跑 `wv_media.py all` 或触发 `/api/ingest` |
| 消息时间整体偏移几小时 | `WV_TZ_OFFSET` 没设 | 非中国时区必须设该变量 |
| 会话分裂成两条 | 老版本按显示名当键（已修复） | 升级后首次同步会自动迁移 `conv_id` |
| 端口 8790 打不开 | `WV_BIND_IP` 填错或防火墙 | 确认是 NAS 内网 IP，检查 NAS 防火墙规则 |
| 容器 unhealthy | 健康检查探不通 | `docker logs wechat-vault`；`/healthz` 会返回 `{"ok":true,"db":"ok"}` |
| 磁盘持续增长 | 媒体/归档未清理 | 检查 `/data/media`、`/data/raw-snapshots`、`/data/logs` |

---

## 工程化

```bash
bash tools/preflight.sh                      # 提交前体检（阻断项必须为 0）
python -m unittest discover -s tests/unit -t .   # 全量单元测试
python -m compileall -q archiver viewer tools plugins   # 语法检查
node --check viewer/static/app.js            # 前端语法
```

- **`tools/preflight.sh`**：凭据扫描 / 语法 / 静默失败 / 依赖 / SQL 注入面 等 8 类自动检查
- **单元测试**：覆盖解密正确性、会话键迁移、密钥契约、鉴权、媒体协议白名单等高风险点
- **审查标准**：见 `docs/代码审查标准与流程.md`、`docs/审查报告_2026-10-02.md`

---

## 目录结构

```
wechat-vault/
├── parser/          # 通用解析器（CSV/JSON/HTML/TXT）
├── archiver/        # 归档 + 解密 + 关联 + WxAM + 同步（wv_sync / wv_media / wv_dat / wv_io）
├── viewer/          # FastAPI 后端 + 原生前端（无框架）
├── plugins/
│   └── wv-keymate/  # 密钥管家插件（取钥→校验→投递一条龙）
├── deploy/          # Dockerfile + compose + 微信运行时脚本 + Windows 侧同步脚本
├── tests/           # 单元测试 + 端到端 + 真实数据回归
├── tools/           # preflight 体检、密钥探测、一键部署
├── docs/            # 计划书、验收报告、审查报告
├── env.example      # 环境变量模板
├── requirements.txt          # 查看器依赖
└── requirements-archiver.txt # 归档/解密依赖
```

---

## 安全

- **网段白名单**：`WV_ALLOW_CIDRS` 只放行指定网段，其余 403；支持 IPv6
- **访问密码**：PBKDF2-SHA256 60 万轮 + 无状态 HMAC Cookie + 登录限流（5 次锁 300s）
- **会话管理**：7 天 TTL + 滑动续签 + 单点吊销 + epoch 全局踢人
- **媒体链接白名单**：只放行 `http(s)://` 与站内绝对路径，挡 `javascript:` / `data:` XSS
- **`/healthz`** 唯一无认证端点，真实检查数据库可读性，不含任何数据
- **隐私**：不做任何外发请求；Service Worker 不缓存聊天数据
- **密钥文件**：`local_keys/` 与 `keymate_out/` 已 `.gitignore`；工具日志只露密钥前 6 位

> ⚠️ **面板绝不要暴露到公网**。能看到面板 = 能看到全部聊天记录。

---

## 许可

MIT License（见 `LICENSE`）。

本项目仅用于**个人数据归档**。请遵守当地法律法规与《微信用户协议》。
所含的离线解密代码，使用与传播风险由使用者自行承担。

---
---

# 🤖 给 Agent / 自动化：一键部署 + 自动取钥

> 本章节面向**其他 AI Agent 或自动化脚本**，提供可直接执行的指令序列。
> 人类用户走上面的分步说明即可，本章可跳过。

## 一、一条命令完成部署（NAS 侧）

```bash
bash tools/oneclick_deploy.sh
```

脚本自动完成：环境检查 → 密钥材料装载 → 容器构建/启动 → 健康验收 → 输出状态。
全部失败项会以非零码退出，适合 Agent 判定成败。

环境变量（脚本会读，也可用 CLI 参数覆盖）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WV_NAS_HOST` | `127.0.0.1` | NAS 内网 IP |
| `WV_STORE` | `/vol3/1000/wechat-vault-single` | 数据目录 |
| `WV_SKIP_BUILD` | `0` | 置 `1` 跳过镜像构建（已有镜像时） |

## 二、自动取钥（Windows 宿主机侧）

> ⚠️ **取钥必须在 Windows 宿主机做**（Docker 容器内取不到）。Agent 应在 Windows 侧
> 执行下列命令，产物会自动投递到 NAS。

### 路线 A：内存扫描（微信 ≤ 4.0.3.36）

```powershell
# 1. 开捕获窗口（回车开始）→ 登录微信 → 打开聊天列表 → 回车停止
python plugins\wv-keymate\extract_scan.py capture `
    --db-dir "C:\Users\<你>\Documents\xwechat_files\<wxid>_<后缀>\db_storage" `
    --out keymate_out

# 2. 校验 + 投递（一条命令）
python plugins\wv-keymate\keymate.py run keymate_out\passphrase.json `
    --db-dir "C:\Users\<你>\Documents\xwechat_files\<wxid>_<后缀>\db_storage" `
    --nas <NAS私网IP>
```

### 路线 B：进程快照（微信 > 4.0.3.36）

```powershell
# 密钥在 DB 打开时才进内存，请在登录初期/触发加载聊天后尽快抓
python plugins\wv-keymate\extract_dump.py dump `
    --db-dir "C:\Users\<你>\Documents\xwechat_files\<wxid>_<后缀>\db_storage" `
    --out keymate_out

python plugins\wv-keymate\keymate.py run keymate_out\passphrase.json `
    --db-dir "..." --nas <NAS私网IP>
```

### 路线 C：已有密钥材料（最快）

```powershell
python plugins\wv-keymate\keymate.py run <64位hex或json文件> `
    --db-dir "..." --nas <NAS私网IP>
```

`keymate.py run` 返回码：`0` 成功，非 0 失败（可直接判定）。

## 三、验收命令（Agent 决策依据）

```bash
# 服务健康（期望 {"ok":true,"db":"ok"}）
curl -fsS http://<NAS_IP>:8790/healthz

# 数据已载入（期望 conv_count > 0）
curl -fsS http://<NAS_IP>:8790/api/status

# 触发同步
curl -fsS -X POST http://<NAS_IP>:8790/api/ingest

# 搜索可用性（期望 total > 0 或命中）
curl -fsS "http://<NAS_IP>:8790/api/search?q=测试&limit=1"
```

判定矩阵：

| 检查 | 通过条件 | 不通过时 |
|---|---|---|
| `/healthz` | HTTP 200 + `"db":"ok"` | 容器未起 / DB 损坏 |
| `/api/status` | `conv_count` > 0 | 密钥未配置或同步未跑 |
| `/api/search` | `total` ≥ 0 且不报错 | FTS 索引缺失，跑 `/api/ingest` |
| `keymate.py run` | 退出码 0 | 密钥无效，重取 |

## 四、WorkBuddy Skill（可选安装）

若在 WorkBuddy 环境中，可将本项目打包为 Skill 供 Agent 直接调用：

```yaml
# skill.yaml（放在 ~/.workbuddy/skills/wechat-vault-deploy/）
name: wechat-vault-deploy
description: 一键部署微信数据方舟并自动取钥。当用户说「部署微信归档」「归档微信聊天记录」
  「一键搞定微信密钥」时使用。
steps:
  - run: bash tools/oneclick_deploy.sh
  - run: python plugins/wv-keymate/keymate.py verify --db-dir <db_storage>
  - assert: curl -fsS http://<NAS_IP>:8790/healthz
```

安装方式（用户侧）：

```bash
mkdir -p ~/.workbuddy/skills/wechat-vault-deploy
cp skill.yaml ~/.workbuddy/skills/wechat-vault-deploy/
```

## 五、Agent 操作准则（务必遵守）

1. **绝不开公网**：所有端口绑内网 IP；`WV_TRUST_PROXY` 留空
2. **绝不在日志输出密钥本体**：工具已内置脱敏，Agent 也不要回显密钥
3. **取钥只在 Windows 宿主机**：Docker 内取钥是死路
4. **优先 `passphrase.json`**：库重建后仍有效；`all_keys.json` 只是兜底
5. **不内置、不代跑第三方取钥工具**：法律与账号风险由用户自担
6. **改动后必跑 `bash tools/preflight.sh`**：阻断项必须为 0
7. **迁移前必备份**：`cp vault.db vault.db.bak-$(date +%Y%m%d)`

## 六、常见 Agent 任务速查

| 用户意图 | Agent 动作 |
|---|---|
| 「部署一下」 | `bash tools/oneclick_deploy.sh` |
| 「取一下密钥」 | 走上文「二、自动取钥」的路线 A/B/C |
| 「同步最新消息」 | `curl -X POST http://<NAS_IP>:8790/api/ingest` |
| 「搜一下 XXX」 | `curl "http://<NAS_IP>:8790/api/search?q=XXX"` |
| 「导出某会话」 | `GET /api/export/conv?conv_id=...&format=html` |
| 「看看状态」 | `curl http://<NAS_IP>:8790/api/status` |
| 「升级了微信，解不开了」 | 重取密钥（大版本换钥），优先 passphrase |
