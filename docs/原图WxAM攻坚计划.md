# 微信数据方舟 · 原图（WxAM/wxgf）攻坚计划表 · v2（可移植版）

> 制定：2026-10-02 15:3x　**版本切换：v1 的 Windows-only 方案作废**
> 定位前提：项目定位 = **B（完整工具链，主流程含解密）**，但**「不假定用户是 Windows」是硬约束**

---

## 〇、v1 错在哪（记录，避免重犯）

v1 的方案 A 要求「Windows + 装了微信 + 版本匹配 + 愿跑辅助脚本」——
这与项目初衷「开源、可 Docker 部署、一个容器一条命令」直接背离，
**是把「本机解法」当成了「产品方案」**。v2 起，**可移植性是第一约束**。

---

## 一、结论先行：**硬骨头已经啃开了，而且不需要腾讯的二进制**

**关键认知**：`wxgf` 容器里装的其实是 **HEVC(H.265) 码流**——静态图是单帧 HEVC，
动画表情是「图像轨 + alpha 遮罩轨」双分区。**HEVC 是标准编码，ffmpeg 原生就能解。**

| 结论 | 实测证据 |
|---|---|
| 容器自带 ffmpeg 且有原生 HEVC 解码器 | `ffmpeg 5.1.6` + `VFS..D hevc` |
| **单帧 wxgf 直接可解** | 随机抽样 60 个：**ffmpeg 成功 30 个**；输出为合法 JPEG，**目视确认为全分辨率无瑕疵原图** |
| 失败的全是动画分支 | 30 个失败样本的 HEVC 起始码均为 5 或 7 个（多分区） |
| 产物质量与体积都不差 | 同一张图：ffmpeg `-q:v 3` → 174 KB；腾讯 DLL 重编码 → 189 KB |
| **零腾讯二进制、零 Windows 依赖** | 全程在 NAS 容器内完成 |
| 交叉验证 | `ppwwyyxx/wechat-dump`（`wechat/wxgf.py`，抽 HEVC 交 ffmpeg）与 `sjzar/chatlog`（`pkg/util/dat2img/wxgf.go`，纯 Go 分区解析 + ffmpeg）两条独立实现同结论 |

**规模**：wxgf 共 **15,436 个 / 1.13 GB**（原图 13,981 + 高清 1,455），全部处理预计 **10–20 分钟**。

---

## 二、三层架构（可移植优先）

```
┌─ Tier 1 · 主路径（容器内，完全可移植，零外部依赖）─────────────┐
│  单帧 wxgf  →  ffmpeg -i pipe:0 -vframes 1 -c:v mjpeg  →  JPEG  │
│  实测覆盖 ≈ 50% 的 wxgf（其余为动画分支）                        │
│  失败/不支持  →  自动降级到已有的缩略图（`_t.dat` 100% 是正常图） │
└──────────────────────────────────────────────────────────────┘
┌─ Tier 2 · 增强（容器内，把覆盖率推到接近 100%）────────────────┐
│  动画 wxgf：识别双分区 → 抽出「图像轨 + alpha 遮罩轨」           │
│    → ffmpeg alphamerge + palettegen/paletteuse → GIF            │
│  参考实现：chatlog `wxgf.go` 的 findDataPartition / LikeAnime   │
└──────────────────────────────────────────────────────────────┘
┌─ Tier 3 · 可选加速（不是主路径，仓库不分发任何腾讯二进制）──────┐
│  若环境里恰好有 Windows 微信的 VoipEngine.dll，可经 ctypes 调用   │
│  （已实测可用，输出略大）→ 仅作为 Tier1/2 的补漏与提速            │
│  接口：`WV_WXAM_DECODER=<cmd> <in> <out>`，无则忽略              │
└──────────────────────────────────────────────────────────────┘
```

**设计要点**
- **默认行为零依赖**：即使 Tier 2/3 都没做，产品依然可用（缩略图）
- **不 vendor 任何微信二进制**：Tier 3 只调用用户自己环境里已有的文件
- **纯函数式解码接口**：`decode_wxgf(bytes) -> bytes | None`，便于测试与替换

---

## 三、分阶段执行表

| 阶段 | 任务 | 产出 | 验收 |
|---|---|---|---|
| **P0** | 前置校验：确认镜像内 `ffmpeg` 可用 + HEVC 解码器在位（当前镜像**尚未安装 ffmpeg，需补进 Dockerfile**） | Dockerfile 补丁 | `ffmpeg -decoders \| grep hevc` 有输出 |
| **P1** | 实现 `archiver/wv_wxam.py`：`decode_wxgf()`（Tier 1）+ 分区探测辅助函数 | 模块 + 单测 | 样本成功率 ≥ 抽样实测值（50%） |
| **P2** | 批量解码 wxgf → `/data/media/orig/<xx>/<md5>.jpg`，断点续传、失败清单 | 原图文件 + `_wxam_report.json` | 成功率 ≥95%（含动画分支）或 ≥50%（仅 Tier1）；失败清单可导出 |
| **P3** | 回填 `vault.db`：只改「当前指向缩略图」的消息 → 指到原图；前端 lightbox 按需加载原图（`onerror` 回退缩略图，零 schema 变更） | SQL + 前端补丁 | 点开=全分辨率；弱网不阻塞列表 |
| **P4** | **Tier 2 动画分支**：实现双分区识别 + alphamerge → GIF | 代码 + 抽样 | 动画样本成功率显著提升 |
| **P5** | 固化与文档：ffmpeg 进 Dockerfile；`wv_wxam` 并入 `wv_media.py` 作为 `wxam` 子命令；README 增「附件原图」章节（含**说明为何需要解码**——修正计划书 §4 那句错误假设） | 代码 + 文档 | 重跑幂等 |

---

## 四、体积与耗时预算（实测推算）

| 项 | 估算 |
|---|---|
| wxgf 输入 | 1.13 GB / 15,436 个 |
| JPEG 产出 | **约 3–5 GB**（ffmpeg q=3 略小于 DLL 重编码） |
| 解码耗时 | 15,436 × ~0.15 s ≈ **40 分钟**（单进程）；4 进程约 10–15 分钟 |
| 镜像增量 | +ffmpeg ≈ **120 MB** |
| NAS 余量 | `/vol3` 尚有 141 GB，充足 |

---

## 五、风险登记

| # | 风险 | 对策 |
|---|---|---|
| R1 | 镜像里没有 ffmpeg | P0 补进 Dockerfile（`apt install ffmpeg`，走清华源） |
| R2 | 动画分支解不出（约 50%） | Tier 2 实现；未实现期间**降级缩略图**，不影响可用性 |
| R3 | ffmpeg 对某些 HEVC profile 不支持 | 探测失败即跳过并记入失败清单；仍可走 Tier 3（若环境有 DLL） |
| R4 | 解码放大体积 | 只保留长边 ≥ 800px 的原图；或输出质量降到 `-q:v 5` |
| R5 | 并发过高拖垮 NAS | 进程池限 2–4；ffmpeg 单次限时 60s |
| R6 | 覆盖已有正常原图 | P3 只更新「当前指向缩略图」的行；动手前备份 `vault.db` |

**回滚**：只新增 `orig/` 目录 + 更新 `media` 列；备份 `vault.db` 即可回退。

---

## 六、验收标准

1. Tier 1 单测：随机 60 个 wxgf，ffmpeg 成功率 **≥ 50%**（对齐实测基线）
2. 抽样 30 张原图**目视合格**：分辨率显著高于缩略图、颜色正确、无花屏
3. `/media/orig/**` HTTP 返回 `image/jpeg`，`file` 判定为合法 JPEG（FFD8 头 / FFD9 尾）
4. 前端：点击 WxAM 图片 → 加载**全分辨率原图**；异常自动回退缩略图
5. 重跑**幂等**（已解出的跳过）
6. **全程不需要 Windows、不需要任何腾讯二进制** —— 这是本版的硬性验收项

---

## 七、本次变更对「项目定位」的影响（B 的代价，需知情）

项目定位已确认为 **B（主流程含解密）**。这意味着公开仓库会包含：
`wv_dat.py`（V2 解密）、`wv_media.py`（密钥离线派生 + 关联）、`wv_wxam.py`（WxAM 解码）、
以及 `deploy/vendor/wxchat-export`（第三方抓 key/解 SQLCipher 工具）。

**客观事实**：同类项目 `lane2077/wechat-decrypt`、`sjzar/chatlog`、`likeflyme/cloudbak`
**均已收到微信函件并被 GitHub DMCA 下架**。这不影响本机自用，
但公开后的下架概率是现实存在的。可选的缓冲做法（**不改变 B 的定位**）：
- 先私有仓库跑通 → 择机公开
- 公开时把「密钥派生」部分拆到独立可选的子目录/子仓库，核心归档器保持独立可用

---

## 八、参考来源（均已交叉验证）

- `ppwwyyxx/wechat-dump` → `wechat/wxgf.py`：`extract_hevc_bitstream_from_wxgf()` + ffmpeg 解码（另有 Android WXGFDecoder 作为无 ffmpeg 时的备选）
- `sjzar/chatlog` → `pkg/util/dat2img/wxgf.go`：分区识别（`findDataPartition` / `LikeAnime` 比例判据）、
  `Convert2JPG`（ffmpeg 单帧）、`ConvertAnime2GIF`（alphamerge + palette）、`Transmux2MP4`（纯 Go 转封装）
- 腾讯 `VoipEngine.dll!wxam_dec_wxam2pic_5`（本机实测 8/8 成功，作为 Tier 3 备选）
- CSDN/AtomGit、52pojie、upxuu、Sarv's Blog（WxAM 格式与调用签名）
