# WeChat Vault · 微信数据方舟

> 把微信聊天记录长期存到 NAS，随时在浏览器里像用微信一样翻看。
> 开源、免费、不占日常登录位、全程走微信官方合规通道。

---

## 这是什么

一个自托管的微信聊天记录**归档 + 查看**系统。对标付费闭源的「微备份（WxBackup）」，但：

- **开源免费**，多微信号零成本（微备份按微信号重复收费）
- **双轨制** ⭐ —— 既存原始备份包（灾难恢复），也存可读记录（随时查看）。
  微备份只做存档轨，两条都做才是超越点
- **合规** —— 用微信官方「聊天记录管理 → 导入与导出」，不解密、不注入、不破解
- **占用低** —— 空闲时零常驻进程，查看时约 150–250 MB
- **永久可读** —— 数据是标准 TXT/HTML/SQLite，不依赖任何第三方服务存活

### 两条轨各干什么

| 轨道 | 内容 | 解决什么 | 可读性 |
|---|---|---|---|
| **可读轨** | 官方导出的 TXT/HTML + 解码后的图片 | 长期查看、全文检索、统计 | ✅ 直接可读 |
| **存档轨** | 手机微信备份包（`.bak`/`BAK_*`）+ `xwechat_files` 全量 | 灾难恢复（手机丢了能还原） | ❌ 需原机恢复 |

**手机和电脑都能备份**：
- 电脑微信 → 导入与导出 → TXT/HTML → 丢进 `inbox/<微信号>/`
- 手机微信 → 导入与导出 → TXT/HTML → 传到 `inbox/<微信号>/`
- 手机微信 → 聊天记录迁移与备份 → 备份包 → 丢进 `inbox/raw/`（存档轨）

---

## 数据从哪来

**关键认知**：微信的聊天记录「查看」有两条路，我们走的是**合规的那条**。

| 路线 | 数据来源 | 能否查看文字 | 法律风险 | 稳定性 |
|---|---|---|---|---|
| ❌ 解密数据库 | 读微信进程内存取密钥 | ✅ | **高**（腾讯发函下架 chatlog/cloudbak） | 微信一更新就失效 |
| ❌ 自己实现备份协议 | 逆向微信私有协议、冒充 PC 端 | — | **高** | 微信一更新就废 |
| ✅ **官方导出** | 微信自带「聊天记录管理」 | ✅ | **零** | 官方功能，永久可用 |
| ✅ **官方客户端接收** | NAS 上跑官方微信接手机备份 | ✅ | **零** | 官方功能，永久可用 |

### 怎么导出（Windows / 手机）

**可读轨（推荐先做这个）**

1. 微信 → 左下角 `≡` → **设置 → 聊天记录管理 → 导入与导出**
2. 选择 **导出聊天记录** → 选 `导出指定聊天记录` 或全部
3. 格式选 **HTML**（含图片）或 **TXT**
4. 导出产物丢进 NAS 的 `inbox/<你的微信号>/`

> 微信 4.1.1+ 支持此功能；4.1.15 已支持文字 + 时间戳 + 发送方 + 图片缩略图 + **语音转文字**。
> 手机端同理：`设置 → 聊天记录管理 → 导入与导出 → 导出到电脑`。

**存档轨（手机备份包）**

手机微信 → `设置 → 聊天记录迁移与备份 → 备份聊天记录到电脑`，
产物是一组 `Backup.db` / `BAK_0_TEXT` / `BAK_0_MEDIA` / … 文件，丢进 `inbox/raw/`。

> ⚠️ **这一步需要 NAS 上有一台"微信"来接备份协议**（协议要求同局域网 + 存在 PC 端）。
> 本项目采用「NAS 上跑 Docker 版官方微信客户端」的方式，**按需唤起、用完即退**。
> 设计文档 3.3.1 有完整技术说明。

---

## 快速开始

### 1. 安装依赖

```bash
pip install fastapi uvicorn
```

### 2. 归档数据（双轨）

```bash
# ① 可读轨：聊天记录（TXT/HTML/CSV/JSON）
#    目录约定：<源目录>/<账号名>/xxx.txt —— 一个子目录 = 一个微信号
python archiver/wv_archiver.py scan --source ./inbox --store ./vault --exclude-dir raw

# ② 存档轨：微信备份包（.bak / Backup.db / BAK_* / xwechat_files 全量）
python archiver/wv_raw.py ingest --source ./inbox/raw --dest ./vault/raw-snapshots \
    --manifest ./vault/MANIFEST.json
```

可读轨这一步会：
- 扫描目录下所有 `.csv` / `.json` / `.html` / `.txt`
- 按子目录名判定**所属微信号**，会话 ID 全局唯一化为 `账号名::会话名`
- 自动识别格式（支持微信官方导出、WeChatMsg、PyWxDump 等各种来源）
- 计算 SHA-256 指纹，**增量入库**（重复运行不会产生重复数据）
- 建立中文全文索引（SQLite FTS5 + trigram）
- 生成 `MANIFEST.json` 完整性清单

存档轨这一步会：
- 识别微信备份包（`Backup.db` / `BAK_*_TEXT` / `BAK_*_MEDIA`，**部分无扩展名**）
- 按日期分代搬进 `raw-snapshots/YYYY-MM-DD/`
- 内容 SHA-256 幂等去重，生成 `archive-index.json` 记录代次

**增量特性**：同一会话多次导出，新消息自动追加，旧消息自动去重。
存档轨同理 —— 重复投同一份备份包不会重复存储。

**冷备**（把热侧整目录镜像到 HDD）：

```bash
python archiver/wv_mirror.py --src ./vault --dst /mnt/cold --exclude logs
```

### 3. 启动查看器

```bash
python viewer/wv_server.py --store ./vault --port 8790
```

打开 `http://127.0.0.1:8790`

---

## 功能

### 查看器

| 功能 | 说明 |
|---|---|
| **微信式聊天界面** | 左右气泡、时间分隔、系统消息居中，跟微信一致 |
| **会话列表** | 头像、最后消息时间、消息条数，支持按最近/条数/名称排序 |
| **全文检索** | 全局搜索（`Ctrl+K`）跨所有会话，命中高亮，点击跳转定位 |
| **会话内搜索** | `Ctrl+F` 在单会话内查找，上下条导航 |
| **分页加载** | 长会话按需加载，不卡顿；跳转时自动加载到目标消息 |
| **统计报告** | 消息总数、收发比例、类型分布、24 小时活跃时段、发言排行 |
| **暗色模式** | 一键切换，偏好持久化 |
| **图片查看** | 点击放大；支持图片/视频/语音/文件等消息类型 |
| **移动端适配** | 响应式布局，手机上就是一个聊天 App |
| **链接识别** | 消息里的 URL 自动变可点击链接 |

### 归档器

**可读轨 `wv_archiver.py`**

| 命令 | 作用 |
|---|---|
| `scan --source <目录> --store <库>` | 扫描并增量导入（核心命令） |
| `scan ... --exclude-dir raw` | 扫描时跳过指定顶层子目录（如存档轨投放口） |
| `stats --store <库>` | 显示归档库统计（含账号分布） |
| `verify --store <库>` | 校验源文件指纹（检测篡改/损坏） |
| `manifest --store <库>` | 重新生成 MANIFEST.json |
| `archive --source <目录> --dest <目录> --days N` | 冷归档搬运（热→冷分层） |

**存档轨 `wv_raw.py`**

| 命令 | 作用 |
|---|---|
| `ingest --source <投放口> --dest <raw-snapshots> [--manifest <路径>]` | 接收原始包并按日期分代 |
| `list --dest <raw-snapshots>` | 列出各代存档内容 |
| `verify --dest <raw-snapshots>` | 校验存档完整性（重算指纹比对） |

**冷备 `wv_mirror.py`**

| 命令 | 作用 |
|---|---|
| `--src <热侧> --dst <冷侧> [--exclude logs] [--no-delete]` | 整目录增量镜像 |

### .dat 附件解码（已集成进归档流程）

微信把图片/视频加密存成 `.dat`。归档时若消息带有 `media` 字段（例如 CSV 的
`MediaPath` 列）且指向 `.dat` 文件，**归档器会自动解码**并落地到 `<库>/media/`，
查看器通过 `/media/<文件名>` 直接展示。

```bash
# 自动：归档时解码（推荐）
python archiver/wv_archiver.py scan --source D:/WeChatExport --store ./vault-store

# 手动：单独批量解码目录
python archiver/wv_dat.py decode --src D:/xwechat_files/xxx/msg --dst ./media

# 探测单个文件格式
python archiver/wv_dat.py probe --file xxx.dat
```

#### ⚠️ 关于「能不能离线解密 .dat」—— 实测定论（2026-10-01）

我们拿 **226 个真实微信样本**（D 盘 2025 老数据 + G 盘 2026 新数据）做了
交叉验证，结论与网上流传的"旧版 .dat 可以直接 XOR 解开"**并不适用于
微信 4.x（Windows）**：

| 观测项 | 实测结果 |
|---|---|
| 文件头 `07 08` 占比 | **226 / 226 = 100%** |
| 单字节 XOR 嗅探"命中率" | **100%**（全部返回同一个 key `0x45`） |
| 强校验（完整 BMP 结构）通过率 | **0 / 226 = 0%** |

**为什么会 100% 假阳性？** 因为微信 `.dat` 以常量 magic `07 08` 开头，
而 BMP 以 `BM`(42 4d) 开头，于是

```
key = 0x07 ^ 0x42 = 0x45     ← 对每一个文件都是同一个值
```

嗅探算法测到的是「文件以 07 08 开头」这个常量事实，**根本不是密钥**。
XOR 之后首 2 字节确实变成 `BM`，但 `BITMAPFILEHEADER` 的
`fsize / off / hdrsize / 尺寸 / planes / bpp` 全是垃圾 —— 不是合法图片。

> 📌 **因此本项目的解码器已改为「强校验」口径**：
> 只有完整结构合法（BMP 头字段自洽、JPG 有 EOI、PNG 有 IEND）
> 才判定成功；否则**如实报告"加密未解"**，不再谎报成功。
> 早期版本会产出 0% 可用的 `.bmp` 假文件，已修正。

| 格式 | 时代 | 加密方式 | 能否离线解 |
|---|---|---|---|
| Old XOR | 旧版（≤2022 附近） | 单字节 XOR | ✅ 能（有真样本时自动嗅探） |
| V1 | 过渡期 | AES-128-ECB + XOR | ⚠️ 需 16 字节固定密钥 |
| **微信 4.x** | **2025-08+** | **会话级运行时密钥** | ❌ **不能**，密钥不在文件里 |

#### 那真实附件怎么拿到？两条可行路线

**路线 A（推荐，合规零风险）—— 走官方导出**

```
微信 → 设置 → 聊天记录管理 → 导入与导出 → 导出到电脑
```

得到的是**明文**聊天记录 + 附件，可直接被本项目的可读轨消费。
这也是本项目的主推路线。

**路线 B（进阶）—— 运行时取密钥**

趁 `Weixin.exe` 运行，从进程内存提取 V2 会话密钥，再离线解密：

```bash
python archiver/wv_dat.py decode --src ./msg --dst ./media \
    --aes-key <32位hex，从内存提取>
```

解码器的 AES 分支已就绪并做好了边界保护（密钥长度不合法会安全回退，
不会抛异常中断整批任务）。

> 支持的导出来源列名：`MediaPath` / `Media` / `Path` / `File` / `MediaFilePath` /
> `图片路径` / `附件` 等（大小写不敏感）。

#### 元数据 `.dat` 会被自动跳过

微信把一批**非媒体**的状态文件也命名成 `.dat`，与图片混在同一棵树里：

| 文件名 | 含义 |
|---|---|
| `alt_name.dat` | 会话别名表 |
| `phoneid.dat` | 手机标识 |
| `detail.dat` | 设备详情 |
| `backup_time.dat` | 备份时间戳 |
| `phone_history.dat` | 历史设备记录 |
| `roam_device_info.dat` | 漫游设备信息 |

解码器会自动识别并跳过，不再刷满 skip 日志、不再浪费 IO。
真实媒体文件的命名规律（已验证）：

```
wxid_*/msg/attach/<32位hash>/<YYYY-MM>/Img/
        <md5>.dat         原图
        <md5>_t.dat       缩略图（thumb）
        <md5>_h.dat       高清（hd）
        <md5>_t_W.dat     缩略图变体
     cache/<YYYY-MM>/Message/<hash>/Bubble/
        <数字>_<时间戳>_b.dat   气泡缓存图
```

---

## 附件原图（WxAM / wxgf）

微信 4.x 把**原图 / 高清图**压成私有容器 `wxgf`（微信内部称 WxAM）。
本项目的还原方式**不需要腾讯的二进制、不依赖 Windows、不做逆向**：

> `wxgf` 头部 12 字节之后就是**标准 HEVC(H.265) 码流**，用容器内的 `ffmpeg`
> 原生 hevc 解码器即可还原成全分辨率 JPEG。

```bash
# ① 先定标账号级密钥（只需一次，结果缓存在 <媒体目录>/_keys.json）
python archiver/wv_media.py keys   --attach <账号>/msg/attach --out <媒体目录>

# ② 解码全部 wxgf 原图，并把结果回填到 media_full
python archiver/wv_media.py wxam   --attach <账号>/msg/attach \
    --account-root <账号根> --vault <vault.db> --out <媒体目录> --plain-dbs <明文库>
```

**要点**
- 必须显式 `-f hevc`（让 ffmpeg 自行探测会因 12 字节 `wxgf` 头而失败）
- 产物写 `<媒体目录>/orig/<xx>/<md5>.jpg`，幂等可重跑
- 回填到 `msg.media_full`；前端**点开大图时才加载**，取不到自动回退缩略图
- 实测：15,436 个 wxgf / 1.13 GB，全部为单帧静态图，分辨率即原始尺寸

## 支持的格式

归档器自动嗅探以下格式，无需指定：

| 格式 | 来源 | 识别特征 |
|---|---|---|
| **CSV** | WeChatMsg / PyWxDump | 表头含 `localId` / `StrContent` / `IsSender` 等 |
| **JSON** | WeChatMsg / 通用数组 | `[{...}]` 或 `{messages:[...]}` |
| **HTML** | 微信官方导出 / WeChatMsg | 含 `class="message"` 等语义容器 |
| **TXT** | 微信官方导出 / 简易格式 | `时间 发送者: 内容` 段落式 |

字段映射支持**别名与优先级**（例如 CSV 同时有 `NickName` 和 `TalkerId` 时优先用显示名）。

---

## 单容器一体化（推荐 · 对标微备份）

> **一个容器 = 一台「微信备份接收机」**。打开面板 → 一键装微信 → 扫码登录 →
> 手机弹出相同的备份页 → WiFi 走官方迁移通道。数据由微信自己交出，**明文落盘，无解密环节**。

### 它是怎么工作的

微备份的本质不是"解密工具"，而是**内嵌了一台微信**：容器里跑着 Xvfb 虚拟显示 +
微信官方 Linux 版 + KasmVNC 网页串流。你扫码登录的是真实微信会话，点备份时手机微信
弹出与电脑端完全相同的备份页面，传输走的是微信官方「聊天记录迁移与备份」通道。

技术方案复用自开源项目 **WechatOnCloud（WOC）**，并修掉它踩过的坑：

| 关键点 | 做法 |
|---|---|
| 显示缩放 | `xsettingsd` 强制 `Xft/DPI=98304`（96×1024），写错会导致微信窗口秒关/黑屏 |
| machine-id | 每实例唯一并持久化（`/config/.wv-machine-id`），防腾讯设备农场风控 |
| 崩溃转储 | 自动清理 `/config/.xwechat/crashinfo`，防磁盘被吃满 |
| 微信本体 | 不打进镜像（保持镜像轻量），首次启动从腾讯 CDN 拉 `WeChatLinux_x86_64.deb`（~200MB，主备 CDN + 断点续传） |
| 桌面看守 | openbox 无任务栏，自动防止窗口被最小化隐藏 |
| autostart 覆盖 | base 镜像只在文件不存在时复制 `/defaults/autostart`，用 `01` 钩子每次启动强制覆盖，保证升级生效 |

### 部署（一个 compose 搞定）

```bash
cd deploy
sudo docker compose -f docker-compose.single.yml up -d
```

首次启动流程：

1. 打开 `http://<NAS_IP>:8790`（方舟面板，账号密码见 `deploy/.env`）
2. 首页「微信运行时」卡片会显示下载/安装进度（仅首次，~200MB）
3. 装好后点「打开微信桌面」（或直接访问 `http://<NAS_IP>:13000`，KasmVNC 串流；
   宿主 3000 常被 NapCat 等服务占用，故默认映射到 13000，容器内仍为 3000）
4. 微信窗口里扫码登录你的微信
5. 手机微信 → 聊天记录迁移与备份 → 备份到电脑 → 选这台"电脑"
6. WiFi 传输完成后，明文数据落在 `/data/wechat-home`，进入阶段二适配

### 与旧双容器方案的区别

| 项 | 旧方案（已废弃） | 单容器（本方案） |
|---|---|---|
| 容器数 | 方舟 + WOC 实例 两个 | **一个** |
| docker.sock | 面板挂 sock 动态创建实例 | **不引入**（消灭高危面） |
| 端口 | 8790 + 36080 + N×动态端口 | 8790（面板）+ 3000（桌面） |
| 微信数据 | 实例卷 → 需要跨容器搬运 | 同容器直读 |

### 红线（不变）

- 绝不开公网：8790/3000 只绑内网 IP（`${WV_BIND_IP:-192.0.2.10}`）
- 不占日常登录位：容器按需启停，空闲时 `docker stop` 即可
- 不引入 docker.sock，不做任何注入/解密

---

## NAS 部署（双容器旧方案：飞牛 fnOS 已实测）

### 架构：双轨制 + 热 / 冷分层

```
【可读轨】手机/电脑微信 官方导出 TXT·HTML     【存档轨】手机微信 备份(.bak) / xwechat_files 全量
              │                                            │
              │ SMB / 手机传文件                            │ SMB / NAS 上跑官方微信接收
              ▼                                            ▼
        inbox/<微信号>/                               inbox/raw/
              │ 每日 03:00 自动归档                         │ 每日 03:00 自动归档
              ▼                                            ▼
        vault.db（SQLite + FTS5）                     raw-snapshots/YYYY-MM-DD/
        media/（解码后的图片视频）                      archive-index.json（代次+指纹）
              └────────────────┬───────────────────────────┘
                               │ 归档后自动整目录镜像（排除 logs）
                               ▼
                   HC620 HDD  冷备：与热侧同构的完整副本
```

**为什么是双轨**：存档轨解决"手机丢了能还原"，可读轨解决"随时能查能搜"。
微备份只做存档轨 —— 两条都做才是超越点。详见设计文档 3.3。

| 层 | 位置 | 介质 | 作用 |
|---|---|---|---|
| **Docker 引擎** | `/vol2/docker` | SSD + Optane dm-cache | 容器镜像与运行时 |
| **热数据** | `/vol3/1000/wechat-vault` | SSD | 投放口 + 主库 + 索引 + 存档轨 + 媒体 |
| **冷备** | `/vol00/HSH721414ALN6M0/wechat-vault-cold` | HC620 HDD | 整目录同构镜像 |

**热侧目录结构**（设计文档第六章）：

```
/vol3/1000/wechat-vault/
├── inbox/                    ← 投放口（SMB 共享名 = wechat-vault）
│   ├── <微信号1>/            ← 一个子目录 = 一个微信号（多账号隔离）
│   ├── <微信号2>/
│   └── raw/                  ← 存档轨投放口（.bak / Backup.db / BAK_* / xwechat_files 全量）
├── media/                    ← 解码后的图片/视频
├── raw-snapshots/            ← 存档轨，按日期分代
│   └── archive-index.json
├── vault.db                  ← 归档主库（SQLite + FTS5）
├── MANIFEST.json             ← 完整性指纹清单
└── logs/                     ← 运行日志（**不参与冷备镜像**）
```

### 多账号隔离（怎么用）

**一个微信号 = `inbox/` 下的一个子目录**。归档时自动识别，互不干扰：

```
inbox/
├── 主号/          ← 微信 A 的导出文件丢这里
│   ├── 张三.txt
│   └── 项目群.html
├── 备用号/        ← 微信 B 的导出文件丢这里
│   └── 张三.txt   ← 同名会话也不会撞车，库内 conv_id = "备用号::张三"
└── raw/           ← 两个号的存档包都丢这里（存档轨按内容指纹去重）
```

- **不加子目录**也可以：直接躺在 `inbox/` 根的文件归入「默认账号」。
- 库内会话 ID 全局唯一化为 `账号名::会话名`，前端可按账号分栏切换。
- API 全部支持 `?account=<账号名>` 过滤。

### 部署步骤

```bash
# 1. 上传代码到 NAS
scp -r wechat-vault AKI@192.0.2.10:/vol3/1000/

# 2. 建目录（属主必须是运行容器的那个用户，如 1000:1001）
sudo mkdir -p /vol3/1000/wechat-vault/{inbox/raw,media,raw-snapshots,logs}
sudo mkdir -p /vol00/HSH721414ALN6M0/wechat-vault-cold
sudo chown -R 1000:1001 /vol3/1000/wechat-vault /vol00/HSH721414ALN6M0/wechat-vault-cold

# 3. 启动
cd /vol3/1000/wechat-vault/deploy
sudo docker compose up -d
```

打开 `http://<NAS_IP>:8790`。

> **关键**：容器以 **非 root（uid 1000 / gid 1001）** 运行，与宿主用户一致，
> 这样宿主上看到的数据文件属主正常，SMB、文件管理器、冷备目录都不会出现权限问题。
> compose 里通过 `user: "1000:1001"` + Dockerfile 的 `APP_UID/APP_GID` 保证。

### 全自动工作流（双向）

```
┌─ 数据来源 ───────────────────────────────────────────────┐
│  电脑微信：设置 → 聊天记录管理 → 导入与导出 → TXT/HTML     │
│  手机微信：设置 → 聊天记录管理 → 导入与导出 → TXT/HTML     │
│  手机微信：设置 → 聊天记录迁移与备份 → 备份 → .bak 包      │
│       │                                                   │
│       ├─ 可读轨：丢进 inbox/<微信号>/                      │
│       ├─ 存档轨：丢进 inbox/raw/                           │
│       │                                                   │
│       ├─ 定时：Windows 计划任务每天 12:30（robocopy 增量） │
│       └─ 手动：双击桌面 sync-now.cmd                       │
└──────────────┬────────────────────────────────────────────┘
               │ SMB: \\192.0.2.10\wechat-vault\inbox
┌──────────────▼──── NAS 容器 ──────────────────────────────┐
│  手动路径：POST /api/ingest → 两条轨归档 + 重载            │
│                                                            │
│  定时路径：                                                │
│   ① 容器内 scheduler.sh 每分钟轮询                         │
│   ② 到达 03:00 → ① wv_archiver.py scan（可读轨）           │
│                  ② wv_raw.py ingest（存档轨）              │
│   ③ POST /api/reload → 查看器热重载                        │
│   ④ wv_mirror.py 镜像 → HC620 冷备（排除 logs）            │
└───────────────────────────────────────────────────────────┘
```

### HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 库状态（会话数/消息数/账号数） |
| GET | `/api/accounts` | **账号列表**（前端分栏切换器数据源） |
| GET | `/api/conversations` | 会话列表（`?account=` 按账号过滤） |
| GET | `/api/messages` | 会话内消息（`?conv_id=`） |
| GET | `/api/search` | 全文检索（`?q=&account=&conv_id=`） |
| GET | `/api/stats` | 统计报告（`?account=&conv_id=`） |
| POST | `/api/reload` | 重新加载数据库（不归档） |
| POST | `/api/ingest` | **立即归档两条轨 + 重载**（供 Windows 一键投递调用） |
| GET | `/media/{path}` | 解码后的图片/视频（含路径穿越防护） |

**手动随启（不想等 03:00）**：

- Windows：把 `deploy\windows\sync-now.cmd` 和 `sync-now.ps1` 一起拷到桌面，
  双击 `sync-now.cmd` 即可（投递 → 立即归档 → 打开查看页）。
  也可把导出文件夹直接拖到 `sync-now.cmd` 上运行。
- NAS：`sudo sh /vol3/1000/wechat-vault/deploy/scan-now.sh`

> **为什么用 `.cmd` + `.ps1` 组合而不是单个 `.bat`？**
> Windows 批处理以 ANSI/GBK 读取文件，中文注释极易乱码并被解析成命令。
> 因此改用 **`sync-now.cmd`（纯 ASCII 启动器）→ `sync-now.ps1`（UTF-8 BOM，中文安全）**。
> `.ps1` 必须带 **UTF-8 BOM**，否则 Windows PowerShell 5.1 会把中文当 ANSI 读，同样乱码。

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `WV_ROOT` | `/data` | 热侧根目录 |
| `WV_STORE` | `/data` | 归档库目录（vault.db 所在） |
| `WV_INBOX` | `/data/inbox` | 可读轨投放口 |
| `WV_RAW_INBOX` | `/data/inbox/raw` | 存档轨投放口 |
| `WV_RAW_SNAPSHOTS` | `/data/raw-snapshots` | 存档轨归档目录 |
| `WV_MEDIA` | `/data/media` | 解码媒体目录 |
| `WV_LOGS` | `/data/logs` | 日志（`scan.log`） |
| `WV_COLD` | `/data-cold` | 冷备挂载点（留空则关闭） |
| `WV_SCAN_HOUR` | `3` | 每天几点自动归档 |
| `WV_SYNC_COLD` | `1` | 是否自动冷备（1=是） |
| `WV_ARCHIVE_RAW` | `1` | 是否处理存档轨（1=是） |

### 备份与恢复

```bash
# 恢复：冷热同构，直接对拷即可
sudo rsync -a --delete /vol00/HSH721414ALN6M0/wechat-vault-cold/ \
                       /vol3/1000/wechat-vault/ --exclude logs
sudo docker restart wechat-vault
```

归档库是纯 SQLite + 标准文件，**不依赖本工具存活**。即使 Docker 镜像全丢，用任意
SQLite 客户端也能直接读 `vault.db` 拿到全部消息。

---

## 目录结构

```
wechat-vault/
├── parser/
│   └── wv_parser.py        # 通用解析器（CSV/JSON/HTML/TXT → 统一结构）
├── archiver/
│   ├── wv_archiver.py      # 增量归档 + 持久化库 + MANIFEST + 媒体解码
│   ├── wv_dat.py           # .dat 附件解码（三代格式）
│   └── wv_mirror.py        # 冷备镜像（纯 Python，容器内无需 rsync）
├── viewer/
│   ├── wv_server.py        # FastAPI 后端（含 /media 静态服务）
│   └── static/             # 前端（原生 HTML/CSS/JS，无框架）
│       ├── index.html
│       ├── app.css
│       └── app.js
├── samples/                # 仿真数据（4 种格式，用于试跑）
├── deploy/
│   ├── docker-compose.yml
│   ├── Dockerfile
│   ├── entrypoint.sh       # 入口：初始化 + 内置调度器 + 前台查看器
│   ├── scan-now.sh         # NAS 端手动立即归档
│   ├── purge-all.sh        # 一键清空全部数据（重置）
│   └── windows/
│       ├── sync-now.ps1           # 一键投递 + 触发归档（UTF-8 BOM）
│       ├── sync-now.cmd           # 双击启动器（纯 ASCII）
│       └── install-autosync.ps1   # 安装 Windows 计划任务
├── final_acceptance.py     # 全链路验收脚本（38 项）
└── make_samples.py         # 生成仿真数据
```

归档库结构：

```
vault-store/
├── vault.db          # SQLite 主库（消息 + 会话 + 全文索引 + 指纹）
├── media/            # 解码后的图片/语音/视频（可选）
└── MANIFEST.json     # 完整性清单（可用于校验与迁移）
```

---

## 自测

```bash
# ① 全链路验收（解析 / 归档 / 解码 / API / 前端 / 一致性，共 38 项）
python make_samples.py
python archiver/wv_archiver.py scan --source samples --store ./store
python final_acceptance.py

# ② 多账号隔离 + 双轨端到端（自造样本，全自动，无需外部依赖）
python tests/test_e2e_multiacct.py

# ③ 旧库自动迁移（验证旧 schema 升级不丢数据、幂等）
python tests/test_olddb_migrate.py

# ④ 安全 + 导出 + PWA（认证 / 限流 / 网段白名单 / HTML+CSV+TXT 导出 / 可安装）
python tests/test_security_export_pwa.py

# ⑤ 真实数据踩坑回归（42 项：假 magic、垃圾文本、正则假阳性、元数据误判）
python tests/test_real_data_regressions.py

# ⑥ NAS 实机部署 + 验收（需 NAS 可达；push 会重建容器）
python tests/_nas_full.py all
```

前五套都是**全自动**的：自建临时目录、跑完自动清理，不碰你的真实数据。

### 针对你本机真实微信数据的兼容性测试（需插上/挂载你的盘）

```bash
# 新旧数据兼容性抽样（只读源目录，输出 compat_real_report.json）
python tests/compat_real_data.py

# 端到端导入实测：复制切片 → 导入 → 幂等复测 → 解码（只读源）
python tests/compat_e2e_import.py

# 决定性验证：.dat 到底能不能离线解（输出证据表）
python tests/prove_dat_encryption.py

# 解码产物强校验 + 全 256 密钥穷举（识破假阳性）
python tests/verify_decoded_images.py
```

> 上面这几个脚本默认读 `D:\xwechat_files` 与
> `G:\We chat and QQ\Wechat\xwechat_files`，**全程只读，只写工作区沙箱**。
> 路径不同请改脚本顶部的 `SOURCES`。

---

## 已实测结论

在飞牛 fnOS（i3-8320 / 18GB / SSD 热数据 + HC620 14T 冷备）上实机部署验证：

| 能力 | 实测结果 |
|------|---------|
| 可读轨归档 | 4 会话 / 25 消息 / 2 账号，解析正确 |
| **多账号隔离** | 不同微信号下的同名会话独立计数（「家人群」9 条 vs 5 条） |
| **存档轨归档** | 3 个备份包按日期分代；**无扩展名的 `BAK_0_TEXT`/`BAK_0_MEDIA` 正确识别** |
| 增量幂等 | 二次投放全部识别为「重复跳过」 |
| 冷备镜像 | 整目录同构镜像，`logs/` 正确排除，增量跳过生效 |
| 全文搜索 | 中文关键词命中并高亮 |
| 旧库迁移 | schema 自动升级，数据零丢失，可重复执行 |
| **访问密码** | PBKDF2-SHA256 60 万轮 + 无状态签名 Cookie；未登录页面 302、API 401 |
| **登录限流** | 同一 IP 错 5 次锁定 300 秒，前端显示剩余次数 |
| **网段白名单** | 非允许网段直接 403；`127.0.0.1` 可单独放行供容器健康检查 |
| **导出** | 单会话 / 按账号 / 全库，HTML · CSV（带 BOM）· TXT · JSON 清单 |
| **PWA** | 可安装到桌面；外壳缓存、**聊天数据不缓存**（隐私优先） |
| 完整验收 | **38 / 38 通过**；安全+导出+PWA **58 / 58 通过** |

### 真实微信数据兼容性（226 个真实样本，2026-10-01）

| 观测项 | 老数据（D 盘 2025） | 新数据（G 盘 2026） |
|---|---|---|
| `.dat` 抽样数 | 157 | 69 |
| 文件头 `07 08` 占比 | **100%** | **100%** |
| XOR 嗅探"命中" | 100%（key 恒 `0x45`） | 100%（key 恒 `0x45`） |
| **强校验通过率** | **0%** | **0%** |
| 官方备份包 | `Backup.db` + `BAK_*`，`RMFH` magic | `Backup/<wxid>/<hex>/`，`RMFH` magic |
| 消息索引 `ChatPackage` | 有 | 有 |

**结论：两代数据的 `.dat` 都是真加密，离线解不开。** 归档器已改为如实报告，
不再产出假 `.bmp`。真正的附件必须走官方导出（路线 A）。

### 归档器把关（防垃圾入库）

实测发现微信目录树里散落着大量**非聊天记录**的 `.txt`，早期版本会当成
"会话"导入（统计里出现「会话 `01`」「会话 `{(1)」等）。现在会先自证再入库：

| 被正确拒绝 | 被正确接收 |
|---|---|
| clash 配置 `port: 7890...` | 微信官方 TXT 导出 |
| JSON 配置 `{"cache_time":...}` | 微信 4.x CSV（`localId,TalkerId,...`） |
| 3KB 网页壳 `01.html` | WeChatMsg JSON（`sender`/`time_str`） |
| 转发推广文 / AI 提示词 | HTML 导出（`class="message"`） |

判据针对不同类型分别设计（结构化格式看**内容特征**，纯文本看**时间戳密度**），
不靠首字符一刀切 —— 因为正规 JSON/HTML 导出本来就以 `[` / `<!DOCTYPE` 开头。

---

## 安全

### 网络层（红线：绝不开公网）

- **端口只绑局域网 IP**，不绑 `0.0.0.0`：

  ```yaml
  ports:
    - "${WV_BIND_IP:-192.0.2.10}:8790:8790"
  ```

  > ⚠️ 不要写成 `"8790:8790"` —— 那会绑到所有网卡（含 IPv6 公网地址）。

- **网段白名单**：`WV_ALLOW_CIDRS` 只放行指定网段，其余来源直接 403。
  默认 `192.168.31.0/24,192.168.100.0/24,127.0.0.1/32`。
- **`/healthz`** 是唯一无认证端点，仅返回 `{"ok":true}`，不含任何聊天数据。
  它必须绕过白名单 —— 容器健康检查的源 IP 是容器网段，不在局域网段内。

### 应用层

开启访问密码（推荐，局域网也开）：

```bash
# 1. 生成密码哈希（PBKDF2-SHA256，60 万轮，每密码独立盐）
python viewer/wv_auth.py '你的密码'

# 2. 生成会话签名密钥
python viewer/wv_auth.py --secret
```

把两个值写进 `deploy/.env`：

```ini
WV_PASSWORD_HASH=pbkdf2_sha256$$600000$$...$$...
WV_SECRET=...
WV_ALLOW_CIDRS=192.168.31.0/24,192.168.100.0/24,127.0.0.1/32
```

> ⚠️ **`.env` 里的 `$` 必须写成 `$$`。**
> docker compose 会对 `.env` 做变量插值：`pbkdf2_sha256$600000$<盐>$<哈希>` 会被当成
> `$600000`、`$<盐>` 等变量名吃掉，容器里拿到的哈希被**静默篡改**，密码永远验证失败
> —— 而日志里只会有 `The "xx" variable is not set` 一行 warning，极易漏看。
> 写成 `$$` 后 compose 会还原成单个 `$`。部署脚本已自动处理这一步。

| 防护 | 实现 |
|------|------|
| 密码存储 | PBKDF2-HMAC-SHA256 60 万轮 + 每密码独立 16 字节盐，`compare_digest` 定时安全比较 |
| 会话 | 无状态 HMAC-SHA256 签名 Cookie，`HttpOnly` + `SameSite=Lax`，默认 30 天 |
| 暴力破解 | 同 IP 连续错 5 次锁定 300 秒，前端显示剩余尝试次数 |
| 网段限制 | CIDR 精确判定；IPv6 解析失败时放行（避免误锁） |
| 数据完整性 | 归档库**只增不改**，每条消息带来源文件与内容哈希，可追溯 |
| 隐私 | 不做任何外发请求；**Service Worker 不缓存聊天数据**，离线时 API 直接返回 503 |

> ⚠️ **面板绝不要暴露到公网**。能看到面板 = 能看到你的全部聊天记录。
> 即使有密码，也请保持「局域网 + 白名单」双保险。

---

## 常见问题

**Q: 为什么不用 chatlog / cloudbak？**
A: 它们通过读取微信进程内存取密钥来解密数据库，已被腾讯发函下架。本工具走官方导出通道，不受影响。

**Q: 能备份手机上的记录吗？**
A: 能。手机微信 → 设置 → 聊天记录管理 → 导入与导出 → 导出到电脑，产物同样可以归档。

**Q: 导出的文件里没有图片怎么办？**
A: 微信官方导出默认带图片缩略图。若要完整图片，把导出的 `MediaPath` 列指向
`msg/` 目录下的 `.dat` 文件，归档器会自动解码落地到 `media/` 并在界面展示。

**Q: 数据会丢吗？**
A: 归档库是标准 SQLite，MANIFEST 记录所有源文件指纹。建议把 `vault-store/` 定期同步到冷存储（如 NAS 的 HDD 卷）。

---

## 许可

本项目仅用于个人数据归档。请遵守当地法律法规与微信用户协议。
