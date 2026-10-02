#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成仿真微信导出样本，用于验证 WeChat Vault 查看器。
不是真实数据，仅用于功能测试。
"""
import json, os, random, csv
from datetime import datetime, timedelta

OUT = os.path.join(os.path.dirname(__file__), "samples")
os.makedirs(OUT, exist_ok=True)

random.seed(42)

CONTACTS = [
    ("张三", ["吃饭了吗？", "今天天气不错", "那个文件我发你了", "周末有空吗，一起吃个饭",
              "收到，谢谢！", "方案我看了，整体没问题", "明天几点开会？", "好的没问题"]),
    ("李四", ["老板催得紧啊", "这周能交付吗", "我已经改完了", "麻烦你再确认一下",
              "OK 收到", "数据我核对过了", "有问题随时找我"]),
    ("王小美", ["在吗", "帮我看看这个", "哈哈哈哈哈", "好烦啊今天", "晚上出来玩吗",
                "算了不去了", "你真好😊"]),
    ("家人群", ["妈，我到学校了", "记得穿厚点", "晚上回来吃饭吗", "买了你爱吃的",
                "照片发群里了", "这个菜怎么做的？"]),
    ("工作群·技术部", ["服务器又挂了", "谁在看这个问题？", "我来看", "已经重启了",
                       "日志我贴一下", "这个问题是配置写错了", "解决了，感谢！"]),
]


def gen_conv(name, lines, n, fmt):
    """生成一个会话的数据。"""
    base = datetime(2026, 6, 1, 9, 0, 0)
    msgs = []
    t = base
    for i in range(n):
        t += timedelta(minutes=random.randint(1, 180))
        txt = random.choice(lines)
        is_self = random.random() < 0.45
        sender = "我" if is_self else name
        # 偶尔插一条图片/系统消息
        r = random.random()
        if r < 0.06:
            txt, typ = "[图片]", "image"
        elif r < 0.09:
            txt, typ = "[语音]", "voice"
        elif r < 0.11:
            txt, typ = f"{name}撤回了一条消息", "system"
        else:
            typ = "text"
        msgs.append({
            "id": i + 1, "sender": sender, "is_self": is_self,
            "ts": int(t.timestamp()),
            "time_str": t.strftime("%Y-%m-%d %H:%M:%S"),
            "content": txt, "type": typ, "talker": name,
        })

    if fmt == "csv":
        p = os.path.join(OUT, f"{name}.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["localId", "TalkerId", "Type", "IsSender",
                        "CreateTime", "StrTime", "StrContent", "NickName", "Remark"])
            for m in msgs:
                w.writerow([m["id"], name, m["type"], int(m["is_self"]),
                            m["ts"], m["time_str"], m["content"],
                            m["sender"], name])
    elif fmt == "json":
        p = os.path.join(OUT, f"{name}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=1)
    elif fmt == "html":
        p = os.path.join(OUT, f"{name}.html")
        rows = []
        for m in msgs:
            cls = "message self" if m["is_self"] else "message"
            rows.append(
                f'<div class="{cls}"><div class="time">{m["time_str"]}</div>'
                f'<div class="sender">{m["sender"]}</div>'
                f'<div class="content">{m["content"]}</div></div>')
        html = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
                f'<title>{name}</title></head><body>' + "\n".join(rows) +
                '</body></html>')
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
    else:  # txt
        p = os.path.join(OUT, f"{name}.txt")
        with open(p, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(f'{m["time_str"]} {m["sender"]}: {m["content"]}\n')
    return len(msgs)


fmts = ["csv", "json", "html", "txt", "csv"]
total = 0
for (name, lines), fmt in zip(CONTACTS, fmts):
    n = random.randint(120, 480)
    total += gen_conv(name, lines, n, fmt)
    print(f"  {name:12s} [{fmt:4s}] {n:4d} 条")

print(f"\n共生成 {len(CONTACTS)} 个会话 / {total} 条消息 → {OUT}")
