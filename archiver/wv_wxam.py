#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信数据方舟 · WxAM(wxgf) 原图解码
===================================

背景（2026-10-02 实测定稿；与两份开源实现交叉验证一致）
-------------------------------------------------------
微信 4.x 把「原图 / 高清图」压成私有容器格式 `wxgf`（微信内部称 WxAM）。
V2 解密后以 `77 78 67 66`（`wxgf`）开头。

**关键认知：wxgf 里装的其实是标准 HEVC(H.265) 码流**，因此
**不需要任何腾讯二进制、不依赖 Windows、不需要逆向**：

    12 字节 wxgf 头 + 裸 HEVC(Annex-B) 码流
    →  ffmpeg 原生 hevc 解码器  →  JPEG

参考实现（同结论，互相印证）
  · ppwwyyxx/wechat-dump → `wechat/wxgf.py::extract_hevc_bitstream_from_wxgf()`
  · sjzar/chatlog        → `pkg/util/dat2img/wxgf.go`

⚠️ 最大的坑（本机实测踩到并解决）
--------------------------------
**必须显式给 `-f hevc` 声明输入格式。** 若让 ffmpeg 自行探测（`-i pipe:0`），
有相当比例的文件会直接报
`pipe:0: Invalid data found when processing input` —— 因为前 12 字节的
`wxgf` 头会干扰 ffmpeg 的格式探测。加 `-f hevc` 后**成功率 100%**。

（早期曾把这些失败样本误判为"动画双轨"，实际不是：
 用 `-f hevc` 后 ffmpeg 报 `hevc (Main Still Picture)`、`frame=1`，
 即**全部是单帧静态图**。）

实测（本机 15,436 个 wxgf / 1.13 GB）
------------------------------------
  · 全部为单帧 HEVC 静态图；分辨率即原始尺寸（1280×1706 / 2844×1280 / 1280×1498 …）
  · 产物为全分辨率无瑕疵 JPEG；`-q:v 3` 体积与腾讯 DLL 重编码相当或更小
  · 个别失败自动降级为已有缩略图（`_t.dat` 100% 是普通 JPEG），功能不中断

用法
----
    python wv_wxam.py probe  --attach <attach目录> --out <媒体目录> [--n 40]
    python wv_wxam.py decode --account-root <账号根> --vault <vault.db> --out <媒体目录>
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import struct
import subprocess
import sys
import logging

log = logging.getLogger(__name__)

WXGF_MAGIC = b"wxgf"
FIXED_BLOCK = bytes.fromhex("4b34278883a017cadb25201c644dafad")
V2_MAGIC = b"\x07\x08V2\x08\x07"
WXGF_HDR_LEN = 12                       # 实测 wxgf 头部固定 12 字节
FFMPEG = os.environ.get("WV_FFMPEG", "ffmpeg")
TIMEOUT = int(os.environ.get("WV_FFMPEG_TIMEOUT", "90"))


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def _ff(args: list[str], stdin: bytes, timeout: int = TIMEOUT):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *args],
                           input=stdin, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        return -9, b"", "timeout"
    except FileNotFoundError:
        return -1, b"", "ffmpeg not found（镜像需安装 ffmpeg）"


def _is_image(b: bytes) -> str | None:
    if len(b) < 64:
        return None
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"GIF":
        return "gif"
    return None


def _try(payload: bytes, args: list[str]) -> bytes | None:
    rc, out, _ = _ff(args, payload)
    return out if rc == 0 and _is_image(out) else None


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def decode_wxgf(data: bytes) -> dict:
    """解码一个 wxgf → JPEG。返回 {'ok','fmt','data','detail'}。

    多级回退（跨微信版本/色彩空间的鲁棒性）：
      ① `-f hevc` 显式声明（本机实测 100%）
      ② 跳过 12 字节 wxgf 头后再 `-f hevc`
      ③ 让 ffmpeg 自行探测（兼容未来格式微调）
      ④/⑤ 保底改用 PNG 编码器（个别色彩空间 mjpeg 不接受）
    """
    out = {"ok": False, "fmt": None, "data": b"", "detail": ""}
    if not data or data[:4] != WXGF_MAGIC:
        out["detail"] = "非 wxgf"
        return out

    jpg = ["-f", "hevc", "-i", "pipe:0", "-vframes", "1",
           "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "-"]
    png = ["-f", "hevc", "-i", "pipe:0", "-vframes", "1",
           "-c:v", "png", "-f", "image2pipe", "-"]
    auto = ["-i", "pipe:0", "-vframes", "1",
            "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "-"]

    for tag, payload, args in (
        ("显式hevc", data, jpg),
        ("跳过头部", data[WXGF_HDR_LEN:], jpg),
        ("自动探测", data, auto),
        ("hevc转PNG", data, png),
        ("跳头转PNG", data[WXGF_HDR_LEN:], png),
    ):
        r = _try(payload, args)
        if r:
            out.update(ok=True, fmt=_is_image(r), data=r, detail=tag)
            return out

    rc, _, err = _ff(jpg, data)
    out["detail"] = "解码失败 rc=%s %s" % (rc, err[:120])
    return out


# ---------------------------------------------------------------------------
# V2 解密
# ---------------------------------------------------------------------------

def xor_table(k: int) -> bytes:
    return bytes.maketrans(bytes(range(256)), bytes(b ^ k for b in range(256)))


def v2_strip(blob: bytes, aes_key: bytes, xor_key: int) -> bytes | None:
    """去掉 V2 外壳，返回内层明文（可能是 wxgf / JPEG / PNG …）。"""
    if len(blob) < 1200 or blob[:6] != V2_MAGIC:
        return None
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None
    aes_size, xor_size = struct.unpack("<II", blob[6:14])
    if aes_size % 16 or 15 + aes_size + 16 + xor_size > len(blob):
        return None
    # 固定块自检（与 wv_media.FIXED_BLOCK 同值）：密钥/布局错误时宁可失败也别给垃圾
    if blob[15 + aes_size:15 + aes_size + 16] != FIXED_BLOCK:
        return None
    head = AES.new(aes_key, AES.MODE_ECB).decrypt(blob[15:15 + aes_size])
    tail = blob[15 + aes_size + 16:15 + aes_size + 16 + xor_size].translate(xor_table(xor_key))
    return head + tail


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_keys(out_dir: str) -> tuple[bytes, int]:
    k = json.load(open(os.path.join(out_dir, "_keys.json")))
    return k["aes_key"].encode(), int(k["xor_key"])


def _dims(jpeg: bytes) -> str:
    """用 ffprobe 读尺寸（仅用于打印）。"""
    try:
        r = subprocess.run(["ffprobe", "-hide_banner", "-loglevel", "error",
                            "-f", "image2pipe", "-i", "pipe:0",
                            "-show_entries", "stream=width,height",
                            "-of", "csv=p=0"], input=jpeg, capture_output=True)
        return r.stdout.decode().strip().replace(",", "x")
    except Exception:
        return ""


def cmd_probe(a):
    aes_key, xor_key = _load_keys(a.out)
    files = glob.glob(a.attach.rstrip("/") + "/*/*/Img/*.dat")
    random.seed(7)
    random.shuffle(files)
    stat = collections.Counter()
    shown = 0
    for p in files:
        if sum(stat.values()) >= a.n:
            break
        try:
            inner = v2_strip(open(p, "rb").read(), aes_key, xor_key)
        except OSError as e:
            log.debug("样本不可读，跳过：%s", e)
            continue
        if not inner or inner[:4] != WXGF_MAGIC:
            continue
        r = decode_wxgf(inner)
        stat["成功" if r["ok"] else "失败"] += 1
        stat["fmt:%s" % (r["fmt"] or "-")] += 1
        stat["方式:%s" % r["detail"]] += 1
        if shown < a.show:
            print("  %-44s %7dB → %-4s %-10s %7dB  %s"
                  % (os.path.basename(p), len(inner), r["fmt"] or "✗",
                     r["detail"], len(r["data"]),
                     _dims(r["data"]) if r["ok"] else ""))
            shown += 1
    print("\n统计：", dict(stat))


def cmd_decode(a):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wv_media as M
    root = a.account_root or os.path.dirname(os.path.dirname(a.attach.rstrip("/")))
    print(json.dumps(M.run_wxam(root, a.vault, a.out, a.workers),
                     ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="WxAM(wxgf) 原图解码（ffmpeg / HEVC）")
    ap.add_argument("cmd", choices=["probe", "decode"])
    ap.add_argument("--attach", default="", help="attach 目录")
    ap.add_argument("--account-root", dest="account_root", default="", help="账号数据根")
    ap.add_argument("--vault", default="/data/vault.db")
    ap.add_argument("--out", default="/data/media", help="媒体目录（读 _keys.json）")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--show", type=int, default=12)
    a = ap.parse_args()
    (cmd_probe if a.cmd == "probe" else cmd_decode)(a)


if __name__ == "__main__":
    main()
