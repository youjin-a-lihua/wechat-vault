#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
真实数据踩坑回归测试
====================

锁死两个**在真实微信数据上暴露、且已修复**的 bug，防止回归：

  BUG-1  假 V1 magic 导致 AES 越界崩溃
         Windows 微信 4.x 的 .dat 首字节随机，1/65536 概率撞上 V1_MAGIC
         (07 08)。旧代码只看 2 字节 magic 就跳 AES 分支 →
         _expand_key 抛 IndexError → 整批解码中断。

  BUG-2  垃圾文本被当成"会话"导入
         微信目录树里散落 clash 配置 / json 配置 / 网页壳 / 推广文，
         旧代码对所有 .txt/.html/.json/.csv 一网打尽 →
         统计里出现「会话 01」「会话 {(1)」「会话 19851f698aa.yaml」。

  BUG-3  时间戳正则假阳性
         `20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}` 会把"2026年度综合工薪补贴"
         的前几个字误判成日期。

  BUG-4  元数据 .dat 被送进图片解码器
         alt_name.dat / phoneid.dat / detail.dat 等是状态文件，不是媒体，
         强行解码只会刷满 skip 日志。

运行：python tests/test_real_data_regressions.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archiver"))

import wv_archiver as A  # noqa: E402
import wv_dat as D  # noqa: E402

PASS = 0
FAIL = 0


def check(cond: bool, name: str, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main() -> int:
    print("=" * 66)
    print("BUG-1  假 V1 magic 不得导致 AES 越界崩溃")
    print("=" * 66)
    # 构造：首字节 XOR key 恰好让前两字节变成 07 08
    real_payload = b"BM" + b"\x00" * 6 + b"\xff" * 40
    for key in (0x45, 0x42, 0x07 ^ 0x42, 0x08 ^ 0x42):
        blob = bytes(b ^ key for b in real_payload)
        try:
            r = D.decode_dat(blob)
            check(True, f"key=0x{key:02x} 未崩溃", f"ok={r['ok']} fmt={r['fmt']}")
        except Exception as e:
            check(False, f"key=0x{key:02x} 未崩溃", f"抛异常 {type(e).__name__}: {e}")

    # 精确复刻现场：07 08 开头 + 高位全 0（Old XOR 典型特征）
    evil = bytes([0x07, 0x08, 0x00, 0x00]) + b"\x11" * 60
    try:
        r = D.decode_dat(evil)
        check(True, "07 08 00 00 开头不崩溃", f"ok={r['ok']} fmt={r['fmt']}")
    except Exception as e:
        check(False, "07 08 00 00 开头不崩溃", f"{type(e).__name__}: {e}")

    # _looks_like_aes_v1v2 判据本身
    check(D._looks_like_aes_v1v2(b"\x07\x08\x00\x00" + b"\x11" * 20) is False,
          "_looks_like_aes_v1v2 拒绝高位零串")
    check(D._looks_like_aes_v1v2(b"\x07\x08\xab\xcd" + b"\x11" * 20) is True,
          "_looks_like_aes_v1v2 接受真密文特征")
    check(D._looks_like_aes_v1v2(b"\xff\xd8\xff\xe0" + b"\x11" * 20) is False,
          "_looks_like_aes_v1v2 拒绝非 magic")
    try:
        D._expand_key(b"short")
        check(False, "_expand_key 拒绝错误长度密钥", "居然没抛异常")
    except ValueError:
        check(True, "_expand_key 拒绝错误长度密钥")
    except Exception as e:
        check(False, "_expand_key 拒绝错误长度密钥", f"抛了 {type(e).__name__}")

    print()
    print("=" * 66)
    print("BUG-4  元数据 .dat 不得送进图片解码器")
    print("=" * 66)
    for name in ("alt_name.dat", "phoneid.dat", "detail.dat",
                 "backup_time.dat", "phone_history.dat", "roam_device_info.dat"):
        check(D.is_metadata_dat(name) is True, f"{name} 判为元数据")
    for name in ("02266332dd8c68ecc5456a88eeb43b65.dat",
                 "04d6521c3a1d37058abafe784d6dedaa_t.dat",
                 "0922fb2b48f88b0eb7be68c84a99c2f4_h.dat",
                 "6fe7591b64fb75fde3662b2c276ba12e_b.dat"):
        check(D.is_metadata_dat(name) is False, f"{name} 判为媒体")

    print()
    print("=" * 66)
    print("BUG-3  时间戳正则不得假阳性")
    print("=" * 66)
    should_match = [
        "2026-10-01 09:29:00", "2026/10/1", "2026年10月1日", "2026年10月",
        "14:30", "下午2:30", "上午 09:15", "2026-06-01 11:12:00",
    ]
    should_not = [
        "2026年度综合工薪补贴", "2026年综合津贴", "按1000-8000元/人",
        "api_1", "TV-1080资源", "《2026年度综合工薪补贴》",
    ]
    for s in should_match:
        check(bool(A._CHAT_TS_PAT.search(s)), f"命中时间戳 «{s}»")
    for s in should_not:
        check(not A._CHAT_TS_PAT.search(s), f"排除假阳性 «{s}»")

    print()
    print("=" * 66)
    print("BUG-2  垃圾文本不得被当成聊天记录")
    print("=" * 66)
    tmp = Path(tempfile.mkdtemp(prefix="wv_reg_"))

    # 真实抓到的四类垃圾
    junk = {
        "clash.yaml.txt": ("port: 7890\nsocks-port: 7891\nredir-port: 7892\n"
                           "mixed-port: 7893\nallow-lan: false\nmode: rule\n"),
        "jsonconfig.txt": ('{\n "cache_time": 9200,\n "api_site": {\n'
                           '  "api_1": {\n   "name": "TV-1080资源"\n'),
        "webpage.html": ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                         "<title>x</title></head><body><p>hi</p></body></html>"),
        "promo.txt": ("2026年【个人补贴】在线申领：\n"
                      "根据国家人力资源部要求关于落实正常发放"
                      "《2026年度综合工薪补贴》申领办理通知，依据《国家劳动法》\n"
                      "条例针对在职岗位进行薪资补贴、 医保补贴、住房补贴、"
                      "交通补贴 等综合补贴申领认证通知：\n"
                      "（一）补贴标准：视个人情况，按1000-8000元/人的标准"
                      "一次性发放2026年【个人补贴】在线申领：\n"
                      "根据国家人力资源部要求关于落实正常发放"
                      "《2026年度综合工薪补贴》申领办理通知\n"
                      "（三）在岗人员均可领取，未申领人员请在2026年4月23日"
                      "之前认证， 逾期申请视为放弃不再受理。\n"),
    }
    for fn, content in junk.items():
        p = tmp / fn
        p.write_text(content, encoding="utf-8")
        check(not A.looks_like_chat_export(p), f"拒绝垃圾 «{fn}»")

    # 真聊天导出：必须全部收
    good = {
        "chat.txt": "张三\n" + "\n".join(
            f"2026-09-0{i} 10:0{i}:00 张三\n消息内容 第{i}条" for i in range(1, 6)),
        "tiny.txt": "工作群\n2026-09-01 09:00:00 老板\n今天开会\n",
        "wx_export.csv": ("localId,TalkerId,Type,IsSender,CreateTime,StrTime,"
                          "StrContent,NickName,Remark\n"
                          "1,张三,text,0,1780277340,2026-06-01 09:29:00,"
                          "吃饭了吗？,张三,张三\n"),
        "wx_export.json": ('[\n {"id": 1, "sender": "我", "is_self": true,\n'
                           '  "ts": 1780283520, "time_str": "2026-06-01 11:12:00",\n'
                           '  "content": "OK 收到", "type": "text", "talker": "李四"}]\n'),
        "wx_export.html": ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                           '<title>王小美</title></head><body>'
                           '<div class="message self"><div class="time">'
                           '2026-06-01 11:55:00</div><div class="sender">我</div>'
                           '<div class="content">算了不去了</div></div></body></html>'),
    }
    for fn, content in good.items():
        p = tmp / fn
        p.write_text(content, encoding="utf-8")
        check(A.looks_like_chat_export(p), f"接收聊天导出 «{fn}»")

    print()
    print("=" * 66)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
