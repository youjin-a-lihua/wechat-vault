#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档链路的落盘小工具。

只放一件事：**原子写**。

为什么需要它：`_index.json`（媒体尺寸索引）和 `archive-index.json`（存档轨
清单）都是"覆盖式重写"的产物，而且体积不小（媒体索引能到 10 MB 级）。
直接 open(w) 写的话，写到一半进程被 kill（容器重启、OOM、手动停服务）
就留下一个截断的 JSON —— 下次启动解析失败，索引整份失效，表现为
"媒体全丢了"或"存档轨一片空白"，而数据其实还在盘上。

原子写的做法：先写同目录临时文件 → fsync → os.replace 覆盖。
os.replace 在同一分区上是原子的，读者只会看到旧版或新版，不会看到半截。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def atomic_write_text(path: str | Path, text: str,
                      encoding: str = "utf-8") -> Path:
    """原子写入文本：临时文件 → fsync → 替换目标。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        # 写失败就把临时文件收掉，别在索引目录里堆垃圾。
        # 这里吞的是"清理失败"而不是"写入失败"——清理不掉只占一点空间，
        # 让它冒出去反而会盖掉真正的写入异常，所以只记一行日志。
        try:
            os.unlink(tmp)
        except OSError as e:
            log.warning("临时文件清理失败（%s）：%s", tmp, e)
        raise
    return p


def atomic_write_json(path: str | Path, data: Any,
                      ensure_ascii: bool = False, indent: int | None = None) -> Path:
    """原子写入 JSON（见 atomic_write_text）。"""
    return atomic_write_text(path,
                             json.dumps(data, ensure_ascii=ensure_ascii, indent=indent),
                             encoding="utf-8")
