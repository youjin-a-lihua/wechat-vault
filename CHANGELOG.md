# 更新记录

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [Unreleased]

### 新增
- **自动解析：检测到新文件即触发**。调度器每 `WV_AUTO_SCAN_MIN` 分钟（默认 5）
  用 `find -L` 检查投放目录，有变更就走完整链路（文字 → 存档轨 → 媒体 → 重载）。
  可用 `WV_AUTO_SCAN_MIN=0` 关闭。
- **手动解析按钮**：归档面板改为「立即解析（含图片）」，`POST /api/ingest`
  补齐媒体链路（`wv_media.py all` + `extras`），不再只处理文字。
- **解密增量缓存**：`<媒体目录>/_decode_cache.json` 记录每个 `.dat` 的
  `(mtime, size)` 与解密结果，未变更的文件不再读盘解密。
  120021 个文件实测 61.9s → **1.7s**。用 `--force` 可强制全量。
- `_decode_cache.json` 支持手动删除以重建缓存。

### 修复
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
