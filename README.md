# WeChat Vault · 微信数据方舟

> 把微信聊天记录长期归档，随时在浏览器里像用微信一样翻看、全文检索。
> 开源、自托管、多账号零成本。

[![GitHub](https://img.shields.io/badge/GitHub-youjin--a--lihua%2Fwechat--vault-07c160)](https://github.com/youjin-a-lihua/wechat-vault)

---

## 这是什么

一个自托管的微信聊天记录**归档 + 查看**系统，对标付费闭源的「微备份（WxBackup）」。

- **双轨制** ⭐ —— 既存原始备份包（灾难恢复），也存可读记录（随时查看检索）
- **全类型附件** —— 图片（含 WxAM 原图）、视频、语音、文件、表情、头像
- **全文检索** —— 中文全文索引，全局搜索 + 会话内搜索 + 按月历查找
- **占用低** —— 空闲零常驻，查看时约 150–250 MB
- **永久可读** —— 数据是标准 SQLite + 文件，不依赖本工具存活

### 两条轨各干什么

| 轨道 | 内容 | 解决什么 |
|---|---|---|
| **可读轨** | 解密/解码后的聊天记录（文字 + 图片/视频/语音） | 长期查看、检索、统计 |
| **存档轨** | 微信备份包（`.bak` / `BAK_*`）+ `xwechat_files` 全量 | 灾难恢复（手机丢了能还原） |

---

## ⚠️ 关于合规，如实说明

本项目包含**对微信本地加密文件的离线处理**（`.dat` 解密、WxAM 解码、密钥派生）。

- 这些能力仅用于**你自己的、合法的个人数据归档**。
- 请遵守当地法律与《微信用户协议》。
- 同类开源项目（chatlog、cloudbak、wechat-decrypt 等）曾收到腾讯下架函件；
  本项目公开发布，**使用与传播的风险由使用者自行判断和承担**。

> 补充说明：微信自带的「聊天记录管理 → 导入与导出」**无法导出可用的明文数据**
> （尤其是附件原图）。这正是本项目必须做离线解密的根本原因——
> 「完全合规、零解密」的官方通道，实际上拿不到完整的聊天记录（含附件）。

---

## 快速开始（Docker，推荐）

```bash
cd deploy
# 先按 env.example 填好 deploy/.env（绑定 IP、桌面口令等）
sudo docker compose -f docker-compose.single.yml up -d
```

打开 `http://<NAS_IP>:8790`。容器内嵌官方微信 Linux 客户端 + KasmVNC 网页桌面，
扫码登录后即可把客户端里的聊天记录同步进方舟。

### 从微信客户端同步

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

### 首次使用：密钥从哪来

方舟读的是微信自己的 SQLCipher 加密库，**必须先拿到密钥才能解密**。
这一步目前没有「一键搞定」的方案，看你手上有什么：

#### 情况 1：你已经有密钥文件

把两个文件放进 `/data/wx_backup/`：

- `passphrase.json` —— 账号级 password。**优先用这个**：它能为任意库重新派生密钥，
  库被微信重建也不会失效
- `all_keys.json` —— 每个库的 `enc_key` 快照。库被微信重建后这份快照就失效了

`wv_sync.py` 会优先用 `passphrase.json`，没有时才回退到 `all_keys.json`。

#### 情况 2：从运行中的微信进程里取

容器内自带三个工具（`/app/tools/`）：

```bash
# 按库的 salt 在微信进程内存里找 enc_key
docker exec -it wechat-vault /opt/wx-venv/bin/python3 /app/tools/wv_keymem.py \
    --account-dir /config/xwechat_files/<wxid>_<后缀> \
    --db message/message_0.db \
    --out /tmp/keys.json

# 思路相同、扫描策略更宽（salt 锚点 ±256 字节提取候选）
docker exec -it wechat-vault /opt/wx-venv/bin/python3 /app/tools/wv_keyscan.py

# Windows 上一份微信的密钥，用这个（纯 Python，无需第三方依赖）
python tools/wv_winkey.py
```

> **如实说明**：这条路不是开箱即用的。在微信 4.1.23 / Linux 版上实测，
> `wv_keymem` 能找到 salt 的内存锚点（627 个可扫描区间、7 处命中），
> 但提取出的候选 key 都没通过 HMAC 校验——微信把派生入参的存放位置
> 与版本强相关。**换版本可能要重新调试**，这一步需要一点逆向耐心。
> 卡住的话，情况 3 更省事。

#### 情况 3：完全不碰密钥

用第三方工具（WeChatMsg、PyWxDump 这类）在 Windows 上把聊天记录
导出成 JSON / HTML，再按下面「把其他电脑上的聊天记录搬过来」的方案 B 导入。
**这条路不需要任何密钥**，代价是拿到的是导出那一刻的快照，
之后的新消息接不上自动同步。

> 详情见下文「部署」与 `docs/计划书.md`。

### 把其他电脑上的聊天记录搬过来

上面讲的是「本机（容器内）微信登录后自动同步」。如果你的完整历史在**另一台电脑**上，
方舟默认看不到它——它只读容器内 `/config/xwechat_files`。

三条路，按可控程度排序。

#### 方案 A：拷数据目录，单独导入（推荐）

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

> 两批数据写的是同一个 `vault.db`，会话按「账号 + 显示名」归并。
> 所以**两台机器上同一会话的备注/群名要一致**，才会合成一个会话；
> 如果原机器上给某人改了备注，这里就会多出一个会话——不是数据错乱，
> 是同一个人被当成了两个。介意的话导入前把备注对齐。

**可能卡住的地方**：

- **报「缺少列 xxx，可能不兼容」**：两台机器的微信大版本不一致（库结构变了）。
  升级原机器的微信到与容器内相同的大版本再拷，或改走方案 B。
- **报「解密完成 0/24」**：密钥没能跨设备用。微信的 `passphrase.json` 通常
  是账号级的，但不保证所有版本都如此——这种情况改走方案 B。
- **数据量大**：`xwechat_files` 几十 GB 是常态，图片视频占大头。
  先只拷 `db_storage/` 也能导入文字消息，附件以后补。

#### 方案 B：导出成文件再导入

**适用**：拿不到密钥，或者两台机器微信版本对不上。
**代价**：依赖第三方导出工具，且导出的是「当时那一刻」的快照。

1. 在原电脑上用导出工具把聊天记录导成 JSON / HTML / CSV / TXT
   （WeChatMsg、PyWxDump、留痕 这类，选你能跑通的即可）
2. 文件丢进容器的 `/data/inbox/<随便起的账号名>/`
3. 网页上点「立即解析」，或者：

```bash
docker exec wechat-vault /opt/wx-venv/bin/python3 /app/archiver/wv_archiver.py scan \
    --source /data/inbox --store /data
```

字段名会自动嗅探，兼容 WeChatMsg / PyWxDump / 微信官方导出这几种常见格式。

#### 方案 C：用微信自带的「聊天记录迁移」

源电脑微信里有「设置 → 聊天 → 聊天记录迁移与备份」，可以把记录迁到容器里的微信。
好处是走官方通道、不碰密钥；限制是**微信对同一账号多台电脑同时在线的规则
会决定这条路顺不顺**，迁移期间可能要在两台机器之间反复登录切换。

适合只想搬一部分会话的场景。这条路的细节由微信客户端决定，本项目不做干预——
迁完之后容器内的微信有了数据，自动同步会照常接手。

### 本地开发

```bash
pip install -r requirements.txt              # 查看器依赖
pip install -r requirements-archiver.txt     # 归档/解密依赖（含 pycryptodome、zstandard）

# ① 定标账号级密钥（离线派生，结果缓存到 <媒体目录>/_keys.json）
python archiver/wv_media.py keys --attach <账号>/msg/attach --out <媒体目录>

# ② 批量解密图片
python archiver/wv_media.py decode --attach <账号>/msg/attach --out <媒体目录>

# ③ 关联消息 ↔ 图片
python archiver/wv_media.py link --vault <vault.db> --plain-dbs <明文库> --out <媒体目录>

# ④ 解码 WxAM 原图（wxgf → 全分辨率 JPEG）
python archiver/wv_media.py wxam --attach <账号>/msg/attach \
    --account-root <账号根> --vault <vault.db> --out <媒体目录> --plain-dbs <明文库>
```

---

## 核心能力

### 归档与解密

| 能力 | 说明 |
|---|---|
| `.dat` V2 解密 | 账号级密钥**离线派生**（无需运行微信、无需扫内存），批量解密图片 |
| WxAM / wxgf 原图 | 微信私有压缩格式，内层是**标准 HEVC**，用 ffmpeg 还原全分辨率 |
| 视频 / 语音 / 文件 / 表情 / 头像 | 分别按实测判据关联（见 `archiver/wv_media.py` 顶部注释） |
| 增量幂等 | 重复运行自动去重，不会产生重复数据 |

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
4. 登录后执行上文「从微信客户端同步」的 `wv_sync.py story`
5. 再跑媒体链路（`wv_media.py`）把图片、视频、语音接上

> 也可以走「手机微信 → 聊天记录迁移与备份 → 备份到电脑」把历史记录迁进客户端，
> 之后再执行 `wv_sync.py story` 同步。两条路最终都汇入 vault.db。

### 红线

- **绝不开公网**：端口只绑内网 IP（`WV_BIND_IP`），默认网段白名单
- 不引入 `docker.sock`
- 空闲时 `docker stop` 即可，不占日常登录位

### 环境变量（`deploy/.env`，参考 `env.example`）

| 变量 | 说明 |
|---|---|
| `WV_BIND_IP` | 绑定内网 IP（**必填**，缺失即启动报错） |
| `WV_PASSWORD` | KasmVNC 桌面口令（**必填**） |
| `WV_PASSWORD_HASH` / `WV_SECRET` | 查看器访问密码 / 会话签名密钥（可选） |
| `WV_ALLOW_CIDRS` | 允许访问的网段白名单 |
| `WV_MEDIA` / `WV_PLAIN_DBS` | 媒体目录 / 解密明文库目录 |

> ⚠️ `.env` 里的 `$` 必须写成 `$$`（docker compose 会做变量插值）。

### HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 库状态 |
| GET | `/api/conversations` | 会话列表 |
| GET | `/api/messages` | 会话内消息 |
| GET | `/api/search` | 全文检索 |
| GET | `/api/stats` | 统计 |
| GET | `/media/{path}` | 解码后的媒体（含路径穿越防护） |
| POST | `/api/reload` | 重载数据库 |
| POST | `/api/ingest` | 立即归档 + 重载 |

---

## 工程化

本项目已建立系统化的代码质量机制：

```bash
bash tools/preflight.sh     # 提交前体检：凭据扫描 / 语法 / 静默失败 / 依赖 8 类自动检查
python tests/unit/test_wv_media.py   # 解密 + 关联的单元测试（11 个用例）
```

- **`tools/preflight.sh`**：机器可判定的检查，阻断项必须为 0 才可提交
- **单元测试**：覆盖解密正确性、密钥搜索分段、严格校验等高风险点
- **审查标准**：见 `docs/代码审查标准与流程.md`、`docs/审查报告_2026-10-02.md`

---

## 目录结构

```
wechat-vault/
├── parser/          # 通用解析器（CSV/JSON/HTML/TXT）
├── archiver/        # 归档 + 解密 + 关联 + WxAM（wv_media.py / wv_wxam.py / wv_dat.py）
├── viewer/          # FastAPI 后端 + 原生前端（无框架）
├── deploy/          # Dockerfile + compose + 微信运行时脚本
├── tests/           # 单元测试 + 端到端 + 真实数据回归
├── tools/           # preflight 体检脚本、密钥探测等
├── docs/            # 计划书、验收报告、审查报告
├── requirements.txt          # 查看器依赖
└── requirements-archiver.txt # 归档/解密依赖
```

---

## 安全

- **网段白名单**：`WV_ALLOW_CIDRS` 只放行指定网段，其余 403
- **访问密码**：PBKDF2-SHA256 60 万轮 + 无状态 HMAC Cookie + 登录限流（5 次锁 300s）
- **`/healthz`** 唯一无认证端点，不含任何数据
- **隐私**：不做任何外发请求；Service Worker 不缓存聊天数据

> ⚠️ **面板绝不要暴露到公网**。能看到面板 = 能看到全部聊天记录。

---

## 许可

MIT License（见 `LICENSE`）。

本项目仅用于**个人数据归档**。请遵守当地法律法规与《微信用户协议》。
所含的离线解密代码，使用与传播风险由使用者自行承担。
