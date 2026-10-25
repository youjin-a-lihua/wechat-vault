# wv-keymate 密钥管家

把「你已经以某种方式拿到了微信 4.x 账号密钥」之后的 10 分钟变成确定性体验：
**校验 → 规范化 → 投递 NAS → 触发同步**，一条龙跑完，直到下次大版本换钥前自动续用。

本插件是 **微信个人数据导出/归档** 链路的密钥配套件。只做密钥的校验、规整与
内网投递，不改动客户端、不触碰第三方进程内部、绝不开公网。

> **平台提示**：Docker 容器内取不了密钥——密钥只在 Windows 宿主机侧可见。
> 取钥（下面第一步）必须在 Windows 上做；产物投递到 NAS 后，容器里的
> `archiver/wv_sync.py` 自动续用。

---

## 三步决策树

### 第一步：取（三选一）

| 路线 | 做法 | 适用 | 说明 |
|------|------|------|------|
| **① 降级取钥** | 临时安装旧版微信（**≤ 4.0.3.36**）→ 登录 → 跑 `extract_scan.py` 捕获窗口 → 拿到 passphrase 后升回新版 | 首选；愿意临时降级一次 | **密钥在大版本内通用，一次取长期有效**。升回新版后自动续用 |
| **② 自备工具取** | 社区存在各类取钥工具，自行取得密钥材料后，把产物（64 hex passphrase，或 `all_keys.json`）交给第二步 | 不便降级、已有取钥途径 | 本插件**不内置、不代跑、不提供下载指向、不特指任何工具**。法律与账号风险自担，见下方风险告知 |
| **③ 完全不碰密钥** | 走微信自带「导入与导出」导出聊天记录 → 用 `deploy/windows/sync-now.ps1` 投递 JSON 快照 | 只要一次性快照归档 | 接不上自动同步（每有新消息都要重新导出），但零密钥接触 |

路线①的捕获窗口用法：

```powershell
# Windows 宿主机上（可先开好微信登录界面）
python plugins\wv-keymate\extract_scan.py capture --db-dir <xwechat_files\账号\db_storage> --out keymate_out
# 按回车开始捕获 → 登录微信并打开聊天列表 → 按回车停止
# 命中后直接落 passphrase.json / all_keys.json
```

新版微信（> 4.0.3.36）瞬时扫描容易扑空时，改用路线 B 的进程快照（同样非侵入、
只用文档化调试 API 取只读快照，离线扫描）：

```powershell
python plugins\wv-keymate\extract_dump.py dump --db-dir <db_storage> --out keymate_out
# 时机很重要：密钥在 DB 打开时才进内存，请在登录初期/触发加载聊天后尽快抓
```

### 第二步：交（校验 + 规范化 + 投递）

```powershell
# 64 hex passphrase 直接粘贴：
python plugins\wv-keymate\keymate.py run <64位十六进制串> --db-dir <db_storage> --nas <NAS私网IP>

# 或拖入 JSON（passphrase.json / all_keys.json 都认，旧格式自动兼容）：
python plugins\wv-keymate\keymate.py run <json文件路径> --db-dir <db_storage> --nas <NAS私网IP>
```

`run` 一次跑完五步：

1. **导入** —— 64 hex 粘贴 / JSON 文件（自动识别 passphrase 还是 all_keys；
   all_keys 兼容 96hex 拼接与 dict 两种旧格式）
2. **校验** —— 逐库跑**解密裁判**（见下），报告「解开了几个库 / 哪些失败」
3. **规范化** —— 按共享契约写 `passphrase.json`（含元数据）+ `all_keys.json`
4. **投递** —— robocopy 到 `\\<NAS>\wechat-vault\wx_backup\`（SMB 内网），
   随后提示 NAS 端 `chmod 600`
5. **触发同步** —— `POST http://<NAS>:8790/api/ingest`（内网 HTTP）

也可以拆开跑：`keymate.py import / verify / deliver / trigger`。
一键投递脚本：`drop.ps1`（robocopy + 触发 ingest，与 `sync-now.ps1` 同族）。

### 第三步：用（自动续用）

NAS 端 `archiver/wv_sync.py` 读 `/data/wx_backup/` 下的 `passphrase.json`
（账号级 passphrase，优先）与 `all_keys.json`（逐库 enc_key 快照，兜底），
自动续跑同步，**直到下次大版本换钥**。换钥后回到第一步重取一次即可。

---

## 校验裁判（唯一判据：实证解密器）

历史上各工具的 HMAC 校验公式互相矛盾，是假阴性的根源。本插件**不用 HMAC
猜谜**，只认一条：**真的把库解开、sqlite 能试开，才算密钥有效**。

```
1. 取库文件前 16 字节为 salt
2. passphrase：aes_key = PBKDF2-HMAC-SHA512(passphrase_bytes, salt, 256000, dklen=32)
   enc_key   ：aes_key = bytes.fromhex(enc_key)      # 已派生，直接用
3. SQLCipher 4 页模型：4096B/页，reserve 80B
   页 0   [0:16) 盐 | [16:4016) 密文 | [4016:4032) IV | [4032:4096) HMAC
   页 n>0 [0:4016) 密文 | [4016:4032) IV | [4032:4096) HMAC
   每页独立 AES-256-CBC，IV 在页尾；reserve 区不参与解密
4. 裁判：解出明文后 sqlite3.connect() 试开并读 schema，能开即密钥有效
```

## 密钥契约（与消费端共享，不得偏离）

`all_keys.json`：

```json
{ "db_dir": "...", "keys": { "message/message_0.db": {"enc_key": "<64 hex>", "salt": "<32 hex>"}, ... } }
```

- `enc_key` = 64 hex = **已派生的** AES key，直接用于 AES，不再走 KDF
- `salt` = 32 hex = 该库前 16 字节

`passphrase.json`：

```json
{"passphrase": "<64 hex = 32 字节账号级 password>", "wxid": "wxid_xxx",
 "captured_at": "ISO8601", "source": "scan|dump|manual", "wechat_version": "4.x.x"}
```

消费端 `load_passphrase()` 只读 `passphrase` 且只校验 `len==64`，元数据字段
天然被忽略、向后兼容。

---

## 风险告知（自行判断）

- **注入类取钥工具**：社区存在通过向客户端进程注入代码取钥的工具品类。此类
  做法可能触发账号风控（社区无定论），并有法律风险（DMCA 反规避条款；2026-01
  曾有大规模下架先例）。**本插件不内置、不代跑、不提供任何此类工具的代码、
  名称或下载指向**；是否使用、后果如何，由你自行判断承担。
- 本插件自身只采用**文档化、非侵入**手段：只读内存枚举（路线 A）、系统调试
  API 取只读进程快照（路线 B）。不改客户端、不改其执行路径。
- **绝不开公网**：投递只走 SMB，通知只走内网 HTTP；目标地址必须落在私网
  白名单（`10/8`、`172.16/12`、`192.168/16`、`127/8`），否则直接拒绝。
- **密钥即数据**：`passphrase.json` / `all_keys.json` 等价于你的聊天记录明文
  钥匙，妥善保管，投递后请在 NAS 端 `chmod 600`。日志只打密钥前 6 位+长度，
  勿主动把密钥文件发给任何人。

## 文件一览

| 文件 | 作用 |
|------|------|
| `keymate.py` | 主入口：导入 / 校验 / 规范化 / 投递 / 触发同步 |
| `extract_scan.py` | 路线 A：内存扫描（≤ 4.0.3.36），含捕获窗口模式 |
| `extract_dump.py` | 路线 B：进程快照 + 离线扫描（新版微信） |
| `drop.ps1` | 一键投递密钥包到 NAS 并触发 ingest |
| `plugin.json` | 插件元数据 |

依赖：Python 3.10+ 标准库；解密校验可选 `pycryptodome`（`pip install pycryptodome`）。
