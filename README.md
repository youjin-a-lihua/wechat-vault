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

打开 `http://<NAS_IP>:8790`。一个容器 = 一台「微信备份接收机」：
内嵌官方微信 Linux 客户端 + KasmVNC 网页桌面，手机走官方「聊天记录迁移与备份」通道投递数据。

> 详情见下文「部署」与 `docs/计划书.md`。

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
4. 手机微信 → 聊天记录迁移与备份 → 备份到电脑 → 选这台"电脑"
5. 数据落盘后进入阶段二（导出 → 归档 → 关联 → 解码）

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
