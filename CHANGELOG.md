# 更新记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.3.2] - 2026-10-25

### 变更（破坏性：会话键改了）
- **`conv_id` 改为以 username 为键**：原键是 `账号::显示名`，而显示名会变——
  对方改备注、群改名、备注被清空，下一次同步就算出一个「新」会话。
  **而且 `msg_id = sha1(conv_id|local_id|ctime)` 是从它派生的**，键一变，
  整段历史的所有消息 id 全部失效，被当成新消息原样重插一遍：
  列表里多一行、库里多几万条重复，而且越用越深。
  新键是 `账号::username`（`wxid_*` / `*@chatroom`，微信内部不可变标识），
  显示名降级为 `conv.title` 这个普通字段，怎么改都不影响键。
  - 新增 `migrate_conv_keys()`：老库自动迁移 `conv.conv_id`、`msg.conv_id`，
    并按新键重算 `db-` 前缀的 `msg_id`。`json-` 前缀的 id 是按文件序号算的、
    反推不出原始字段，只迁 `conv_id` 不动 id（它们的定位信息本来就跟着走）。
  - 新增 `conv.username` 列与 `idx_conv_username`；老库由 `migrate_schema` /
    `ensure_schema` 补列（**先补列再建索引**，否则 `CREATE INDEX` 直接报
    `no such column`）。
  - 显示名撞车（实测 4933 个联系人里 199 组同名）时用该会话出现过的
    `sender_username` 认人；仍判不出的**宁可不迁**，记进 `stuck`，
    主循环沿用老键——猜错就是把陌生人的聊天合并到一起，比不迁严重得多。
  - 迁移后整体重建一次 `msg_fts`（逐行改 UNINDEXED 列等于每行全表扫）。
  - JSON 导入链路同步改 `_resolve_cid()`，与新键合并而不是另起一行。
  - 区分两种「没迁走」，别混成一个失败数：
    `legacy` = 显示名在当前微信号里根本不存在（实测 10 个，全是 JSON 导入
    遗留，消息 id 都是 `json-` 前缀、本轮同步不碰，保持老键无风险）；
    `stuck` = 同名多人且判不出（实测 2 个：「梁辉」里混了 3 个 wxid、
    「电信客服专员」里混了 3 个 openim 号，都是早先按显示名合并出的脏数据）。
  - **升级提示**：首次启动会自动迁移。生产库实测（1,365 MB / 189 万条）：
    conv 479 → 479、msg 1,894,727 → 1,894,727（零重复）、消息内容指纹
    完全一致、改写 464 个会话（1,719,560 条消息）、耗时 86 s（单事务）。
    回滚只能靠备份的 `vault.db`，因为 `msg_id` 已被改写。
    **必须先部署新代码再迁移**：旧代码按显示名算键，认不出新键会把历史
    按新 msg_id 再插一遍——这正是本次要根治的问题。

### 安全
- **会话 token 从 30 天不可撤销改为 7 天 + 可撤销**：
  `exp.epoch.sid.sig` 四段签名。
  - 有效期 7 天（`WV_TOKEN_TTL_DAYS` 可调，下限 1 小时），剩余不足 24h 时
    自动续签（滑动续期，长期在用不会突然掉线）。
  - 单 session 登出：`sid` 进吊销名单（`MAX_REVOKED=200` 上限，LRU 淘汰）。
  - 全局踢人：`POST /api/logout {"all":true}` 递增 `epoch`，所有旧 token 立刻失效。
  - HTTPS 反代下自动加 `Secure`（`WV_COOKIE_SECURE=1` 强制）。
  - 老 token（三段式）一律视为无效，升级后需要重新登录一次。
- **媒体链接协议白名单**：`msg.media` / `media_full` 原来直接插进 `src`/`href`，
  库里若存在 `javascript:` / `data:text/html` 这类值，在归档导出模式下
  点一下就是可执行的 XSS。前后端各加一道：后端 `_safe_media()` 只放行
  `http(s)://` 与本站绝对路径（`/(?!/)`，排除 `//evil.com`），
  前端 `safeUrl()` 同规则；`/api/messages`、`/api/search`、导出三条路径全覆盖。
- **代理头不再默认信任**：`_client_ip()` 只在 `WV_TRUST_PROXY=1` 时才读
  `X-Forwarded-For` / `X-Real-IP`。默认直连部署下这两个头是客户端随便写的，
  信了就等于把 `WV_ALLOW_CIDRS` 白名单和「仅本机可 reload」一起送人。
  `/api/reload` 的本机判定改用不可伪造的 `_peer_ip()`（TCP 对端地址）。
- `WV_ALLOW_CIDRS` 支持 IPv6（`2001:db8::/32`、`::1/128`），
  基于 `ipaddress` 解析，v4/v6 分开比对，解析不了一律拒绝（deny by default）。

### 新增
- **查看器日志落盘**：`WV_LOG_FILE` 开启 `RotatingFileHandler`
  （`WV_LOG_MAX_MB` 单文件上限、`WV_LOG_KEEP` 保留份数）。
  autosync 侧同样支持，容器日志另加 compose 级 `json-file` 轮转
  （`max-size: 20m` / `max-file: 3`）——否则长期运行会把磁盘写满。
- **`/healthz` 真检查库**：原来只回一个固定 200，容器 healthy 与数据库
  是否可读毫无关系。现在以 `mode=ro` 打开 vault.db 并 `SELECT 1 FROM conv`，
  失败返回 503，DB 损坏/被锁时 Docker 能真的把容器标成 unhealthy。
- **`wv_io.py`**：`atomic_write_text()` / `atomic_write_json()`
  （同目录 `mkstemp` → `fsync` → `os.replace`）。`media/_index.json` 与
  `raw-snapshots/archive-index.json` 改用它——原来直接覆盖写，
  进程在写一半时被杀就是一个截断的 JSON，下次启动整份索引读不出来。

### 修复
- **两字中文搜索静默失效（生产实测证实）**：`/api/search` 用 `len(q) >= 2`
  决定是否走 trigram FTS，但 **trigram 分词器把文本切成三个字符一组的
  token，短于 3 个字符的查询永远命中 0 条**——它不报错，只返回空。
  于是中文最常用的两个字（快递 / 会议 / 老师 / 吃饭）「成功地」查出 0 条，
  又因为 `fts_usable=True` 永不回落 LIKE，搜索等于完全失效。
  实测 189 万条的库上：`MATCH '快递'` = 0 条，`LIKE '%快递%'` = 1016 条；
  `'羽毛球'`（3 字）则正常命中 2933 条。
  修法：新增 `FTS_MIN_CHARS = 3` 常量，门槛按分词器实际最小长度定；
  短于 3 字回落 LIKE。修复后线上验证「快递」返回 **1017 条**。
  回归测试用内存 FTS5 表直接断言分词器行为，避免再次凭感觉写下限。
- **同步链路缺 `msg_fts` 导致搜索永远全表扫**：从微信库同步建的库
  （没走过 JSON 导入）根本没有全文索引表，`/api/search` 每次都回落
  `LIKE '%词%'` 扫 189 万行。现在 `ensure_schema` 直接建出来，
  并由 `ensure_fts()` 补齐缺口（临时表 + PRIMARY KEY，避免
  `NOT IN (SELECT ...)` 在 UNINDEXED 列上退化成 O(n²)）。
  生产库实测索引从 937,823 行补到 **1,170,659** 行，此前 **232,836 条**
  消息根本搜不到。
- **搜索零命中时回落全表扫**：原判断是 `fts_ok = 命中数 > 0`，
  于是「这个词确实搜不到」被当成「索引不可用」，转去 LIKE 扫全库。
  改为按「FTS 是否可用」判断，与命中数无关。
- **`/api/ingest` 与 `/api/reload` 并发无锁**：两个请求同时跑会往同一个
  `vault.db` 写、互相覆盖缓存。加 `_HEAVY_LOCK`：ingest 拿不到锁直接 409
  （让调用方稍后重试），reload 最多等 20s。
- **前端 DOM 无限增长**：长会话翻到底会积累上万个节点，滚动卡顿到不可用。
  上限 3000 条，超出回收最旧的节点；往回翻到底部时重新拉最新窗口
  （而不是继续往 DOM 里堆）。提示语与「回到今天」同步适配。
- **`_CAL_CACHE` 未失效**：切换会话/入库后日历计数还是旧的。改为按
  `(conv_id, 年月)` 缓存并在 `_invalidate_derived_caches()` 里整体清空。
- **`/api/messages` 排序键**：改用 `idx_msg_conv_ts_full(conv_id, ts, seq, msg_id)`
  覆盖，避免大会话（实测最大 18 万条）走 TEMP B-TREE 全排序。

### 工程
- 新增单测 30 项：`tests/unit/test_wv_auth.py`（token 有效期/续期/吊销/epoch、
  CIDR v4+v6+拒绝、媒体协议白名单）与 `tests/unit/test_wv_sync.py`
  （会话键迁移全矩阵：改名、幂等、`json-` 保留、歧义跳过、歧义由
  `sender_username` 判定、合并、跨账号隔离、`stuck` 回传，
  外加「老库缺 username 列时 `ensure_schema` 不得崩」的回归测试）。
- 日志里的 `print` 全部改走 `logging`，preflight 的静默异常检查清零。

---

## [0.3.1] - 2026-10-25

### 修复
- **发送者与己方角色判定错误（查错了 id 空间）**：`real_sender_id` 指向的是
  message 库自己 `Name2Id` 的 rowid，**不是 contact.db 的 id**——实测自己的
  `contact.id=85`，而在各 message 库的 `Name2Id` 里 `rowid=3`。原实现拿
  contact.db 的 id 去查，等于查错了表。
  后果：从微信库同步来的 55.9 万条消息里 `is_self=1` 的只有 145 条，
  自己发的消息几乎全被当成对方发的，前端左右角色错位；`sender` 名字
  靠消息体前缀兜底才勉强看着对，前缀缺失时直接显示数字 id。
  修法：新增 `read_name2id()` 逐库翻译 sender_id → username，
  `sender` 改为按 username 查备注/昵称，`is_self` 改为 `sender_user == 自己 wxid`；
  `_self_wxid()` 正确提取账号目录里的真实 wxid（原 `split("_")[0]` 只拿到 `"wxid"`）。
  已有记录整条重算（一次修正 1,371,823 条、43 秒），
  全库 `is_self` 分布变为 66.5% 对方 / 33.5% 自己。
- **前端最新消息不显示（消息列表按 `seq` 排序）**：`/api/messages` 用
  `ORDER BY seq` 分页，但库里混着两套互不相干的编号——JSON 导入的是文件内
  序号（实测 `0..6698`），微信库同步的是微信的 `local_id`（实测 `2..6156`），
  两者范围完全重叠，大小关系毫无意义。后果是按「最新 200 条」取消息时
  取到的是 10 月初导入的那批，10 月底同步的新消息被排到后面，
  表现为「前端最新一条停在很早的日期」。
  改为 `ORDER BY ts DESC, seq DESC, msg_id DESC`（seq 只作同秒 tiebreaker），
  `/api/locate` 的定位查询同步修正。
  另外发现 JSON 导入的表头行 `ts` 为 NULL，SQLite 视 NULL 为最大值，
  `seq DESC` 时会被顶到首位冒充「最新」，按 `ts` 排序后自然沉底。
- **群聊正文残留发送者前缀**：`_strip_sender()` 原来只认「冒号+空格」，
  而微信部分群聊消息是「发送者:\n内容」，导致正文里残留 `wxid_xxx:`。
  改为用该库 `Name2Id` 的 username 集合做白名单——前缀命中才剥离，
  避免误伤正文里的普通冒号（「时间: 12:30」）。
  历史数据用 SQL 就地剥离，实测清理 **329,182 条**（占全库 17.4%）。
- **去重键依赖 content 导致重复插入**：原去重键是
  `(conv_id, ts, content)`，而 content 会被重算。拿新 content 比对旧记录
  必然落空，同一条消息会被插第二份（实测踩过一次，多出 390,288 条，
  已精确删除并重算会话计数）。改为「优先 `msg_id`，兜底
  `(conv_id, ts, sender, is_self)`，不含 content」。

---

## [0.3.0] - 2026-10-24

### 新增
- **前端消息自动刷新**：页面每 20s 轮询 `/api/status` 感知后端数据变化，
  有新消息时刷新侧栏并对当前会话增量追加，不再需要手动刷新。
  - 增量按 `data-mid` 逐条去重，单轮最多回溯 3 页（599 条）
  - `msgLoaded` 用总条数涨幅更新，中间窗口（按日期/搜索跳转）也不会漂移
  - 每处 await 后复查当前会话，切换会话时不会把旧会话的消息写进新会话
  - 翻页途中总条数变化则丢弃本轮，避免 offset 基准后移造成重复与空洞
  - 页面隐藏时暂停轮询，回到前台立即补一次
  - `localStorage.wv_autorefresh='0'` 可彻底关闭
- **时区可配置**：新增 `WV_TZ_OFFSET`（默认 8，支持 5.5 / -3.5 这类小数）。
  此前东八区写死在 6 处，非中国时区部署会导致消息时间整体偏移、
  按日期归档与分库匹配错位。非法值自动回退 8。
- **微信版本探测**：入库前抽检 `message_*.db` 的 `Msg_*` 表结构，
  校验当前解析依赖的 5 个列。不匹配时明确报出「缺少哪些列、可能不兼容」，
  而不是让用户对着「解密完成 0/24」或空结果猜。

### 修复
- **群聊同名成员头像串号**：`msg` 表新增 `sender_username`，同步时从
  `real_sender_id` 反查真实账号，前端取头像优先用 username、色块首字仍用显示名。
  实测 4933 个联系人里有 **199 组显示名重名**（如「迪」对应 5 个账号、
  「该账号已注销」被 3 个公众号共用），波及 **49,118 条**群聊消息；
  修复后同名成员取到各自头像。已有数据在下一轮同步时自动补填
  （实测一次补了 1,371,600 条，耗时 43s）。
- **`renderMessages` 的 append 模式缺基准时间**：该分支此前无人调用，
  首次启用会给同日首条消息重复插日期分隔条。

### 工程
- 代码注释全面补齐：为全部核心模块的公开函数与复杂逻辑补上
  docstring 与关键行内说明（AST 扫描从 89 处缺失降到 1 处——
  唯一剩余是镜像 Windows API 结构体的 `ctypes` 定义，无需注释）。
  重点补足两个新模块（`wv_sync` / `wv_autosync`）的模块级设计说明，
  以及 SQLCipher 4 页结构、消息表 md5 反查等算法层注释。
- 新增 `.dockerignore`：排除 `local_keys/`、`store/`、`_shots_*/`、`tests/`、
  `deploy/.env` 等，避免密钥提取日志进入构建缓存，也避免构建上下文膨胀。
- **清理仓库历史中的内部信息**：从全部提交中移除含 NAS 目录结构、
  硬盘序列号、SSH 用户的私有脚本，以及第三方项目（WechatOnCloud）
  的参考源文件（后者同时删除工作区文件，只在 README 致谢）。
  历史已重写，`git log -S` 检索敏感串为 0 命中。

### 变更
- `sw.js` 缓存版本升到 `wv-v1.9.1`：不 bump 的话，
  Service Worker 的 stale-while-revalidate 会让用户首次打开仍用旧脚本。

---

## [0.2.0] - 2026-10-23

### 新增
- **自动同步（`archiver/wv_autosync.py`）**：常驻进程监视微信消息库的
  `(大小, mtime)` 指纹，变化即自动完成「解密 → 入库 → 媒体关联 → 查看器重载」。
  默认轮询 60s、写入稳定判定 30s，避免读到写了一半的库。
  以 s6 longrun 注册（`svc-autosync`），随容器启动、崩溃自拉起，不依赖桌面会话。
  实测一次完整同步 37s。
  - `WV_AUTOSYNC=0` 关闭；`WV_AUTOSYNC_POLL` / `WV_AUTOSYNC_STABLE` 调整节奏
  - 状态存 `/data/logs/autosync.state`（重启后不重复跑已同步过的数据）
  - 日志 `/data/logs/autosync.log`；失败后 `WV_AUTOSYNC_RETRY`（默认 300s）重试
- **从微信客户端直接同步**：`archiver/wv_sync.py story` 一键完成
  「解密数据库 → 读消息写 vault.db → 供查看器展示」。
  微信登录后不必再手工导出，直接从本地加密库取数据。
- **密钥重新派生**：`wv_sync.py` 优先用 `passphrase.json` 里的账号级
  password 为每个库派生密钥，库被微信重建后也能解；无 password 时
  回退到 `all_keys.json` 的 enc_key 快照。
- **`tools/wv_keymem.py`**：基于 salt 锚点在微信进程内存里定位密钥，
  供密钥快照失效时重新提取。
- **收件箱变更检测**：投放目录有新增文件时，走完整链路
  （文字 → 存档轨 → 媒体 → 重载）。与上面的「自动同步」互补：
  前者针对微信客户端数据库，后者针对手工投放的文件。
- **手动解析按钮**：归档面板改为「立即解析（含图片）」，`POST /api/ingest`
  补齐媒体链路（`wv_media.py all` + `extras`），不再只处理文字。
- **解密增量缓存**：`<媒体目录>/_decode_cache.json` 记录每个 `.dat` 的
  `(mtime, size)` 与解密结果，未变更的文件不再读盘解密。
  120021 个文件实测 61.9s → **1.7s**。用 `--force` 可强制全量。
- `_decode_cache.json` 支持手动删除以重建缓存。

### 修复
- **SQLCipher 4 解密算法修正**（`tools/wv_wxdb.py` 此前的实现整体错误）：
  页尾部 reserve 是 80 字节而非 48；IV 存在每页 `[4016:4032]` 而非由页号派生；
  mac_key 用 2 轮 PBKDF2 而非 256000 轮；enc_key 是已派生密钥、直接用于 AES。
  修复后 24 个库全部解密成功，图片关联率恢复到 99% 以上。
- **图片尺寸映射被覆盖导致关联丢失**。`create_time` 只精确到秒，同秒多图时
  `out[(hash, ts)] = sizes` 会互相覆盖。实测 77883 条图片消息里
  有 2054 组冲突、约 3780 条（4.9%）消息的尺寸信息被丢弃。
  现改为列表累积，关联命中率相应提升。
- **`run_videos` 调用了从未定义的 `_video_thumb_len`**，`videos` / `extras`
  一执行就 `NameError`。改用已实现的 `_plain_thumb_len`。
- **`run_avatars_fetch` 从未实现**，导致 `extras` 跑到最后必崩。
  改为检测缺失后跳过并记录，不再中断整个流程。
- **定时归档从未真正运行**。调度逻辑写在 `deploy/entrypoint.sh`，
  但容器 `ENTRY=/init`，该文件无人调用。现已挂到 openbox autostart。
- **`find` 不跟随符号链接**，投放目录是软链时永远检测不到新文件
  （实测 `find` 返回 0、`find -L` 返回 469）。已改为 `-L`。
- **`/api/ingest` 报「收件箱不存在」**：`WV_INBOX` 指向的路径与实际投放目录
  不一致时无兜底提示。现失败信息包含具体路径。
- **`autostart` 的 `set -u` 与子 shell 变量**：主脚本引用子 shell 内定义的变量
  会静默退出，导致后续「拉起微信」等逻辑不执行。

### 性能
- `/api/stats`：原来 6 条独立全表扫描，改为合并聚合并按
  `(conv_id, account)` 缓存（归档 / 重载时失效）。
  冷态 7.9s → 热态 **0.00s**。
- `/api/conversations`：`ORDER BY COALESCE(end_ts,0)` 使索引失效，
  改为直接排序（NULL 在 DESC 下本就排最后）。0.89s → **0.05s**。
- 明文库扫描：`_plain_sizes` 与 `_plain_thumb_len` 逻辑重复，
  合并为 `_plain_media_meta` 单次扫描并缓存。8.5s → **1.1s**。
- 全库导出：新增 `_iter_convs()` 生成器 + `StreamingResponse`，
  内存峰值从「全部消息」降到「单个会话」。
- `/api/export/manifest`：只读 `conv` 表，不再为了拿会话标题
  把 133 万条消息全读进内存。9.8s → **0.02s**。

### 变更
- `deploy/autostart` 中所有路径改为环境变量驱动
  （`WV_STORE` / `WV_INBOX` / `WV_MEDIA` / `WV_PLAIN_DBS` /
  `WV_WECHAT_FILES` / `WV_MEDIA_PYTHON`），不再写死部署路径。
- `docker-compose*.yml`：卷路径改为 `WV_DATA_DIR` / `WV_COLD_DIR` /
  `WV_CONFIG_DIR`；`WV_ALLOW_CIDRS` 默认值改为仅 `127.0.0.1/32`。
- `deploy/scan-now.sh`、`deploy/purge-all.sh`：路径改环境变量。
- **破坏性变更**：升级后需在 `deploy/.env` 中补 `WV_DATA_DIR`
  （以及可选的 `WV_COLD_DIR` / `WV_CONFIG_DIR`），否则 compose 启动会报错提示。

### 工程
- 新增 `.gitattributes`：强制 `*.sh` / `*.py` / `*.yml` 使用 LF。
  `core.autocrlf=true` 时 Windows 克隆会把 `.sh` 转成 CRLF，
  在 Linux 上执行会报 `bad interpreter`。
- **自动同步服务固化进镜像**：s6 服务定义（`deploy/s6/svc-autosync`）
  由 `Dockerfile.single` 末尾 COPY 进镜像，容器重建后自动恢复，
  不再依赖对运行中容器的手工修补。COPY 放在最末尾是为了不击穿
  后面 `apt install sqlcipher/gdb/ffmpeg` 的数百 MB 缓存层——
  镜像重建从十几分钟降到 50 秒。
- **调度器启动自愈**：`/opt/wx-venv` 是启动钩子从数据卷软链出来的
  运行时资产，数据卷未固化时并不存在；原 `run` 脚本直接 exec 会以
  127 退出，s6 以 100ms 级反复重启形成日志风暴。改为脚本内等待
  （60s 重试、不退出），运行时一出现自动接上，无需人工干预。
- 补充两处静默 `except` 的日志：改为「计数 + 只记前 5 条 + 结尾汇总」，
  避免百万级循环刷屏，同时不再吞掉异常。
- `tests/_nas_*.py` 等含真实部署路径的私有脚本移出版本控制并加入 `.gitignore`。

---

## [0.1.0] - 2026-10-02

首个可运行版本。

### 新增
- `.dat` V2 离线解密：账号级密钥派生，无需运行微信、不读取进程内存
- WxAM / wxgf 原图还原：识别内层为标准 HEVC，用 ffmpeg 解码
- 消息 ↔ 媒体关联：基于会话哈希、时间戳与尺寸匹配
- 查看器：微信式聊天界面、会话列表、全文检索、月历回溯、统计报告
- 双轨归档：可读轨（查询用）+ 存档轨（灾难恢复）
- 单容器部署：内嵌官方微信 Linux 客户端 + KasmVNC 网页桌面
- 访问控制：网段白名单、PBKDF2 口令、无状态 Cookie
