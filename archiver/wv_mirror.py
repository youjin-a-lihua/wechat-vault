#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault · 冷备镜像
=======================

把热数据（vol3 SSD 上的归档库）镜像到冷存储（HC620 HDD）。
纯 Python 实现，无需 rsync —— 容器内零额外依赖。

策略：
  - 逐文件比对 mtime + size，只复制新增/变更的（增量）
  - --delete：删除冷端多余文件，保持严格镜像
  - 复制后校验 size，确保完整

用法：
  python mirror.py --src /data/vault-store --dst /data/cold
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _excluded(rel: Path, patterns: tuple[str, ...]) -> bool:
    """判断相对路径是否命中排除规则。

    规则匹配顶层目录名或任意一级目录名，例如 --exclude logs 会排除
    logs/ 及其下所有内容。
    """
    if not patterns:
        return False
    parts = rel.parts
    for pat in patterns:
        pat = pat.strip().strip("/")
        if not pat:
            continue
        if pat in parts:                      # 命中任意一级目录名
            return True
        # 支持简单通配（如 *.tmp）
        if Path(rel.name).match(pat):
            return True
    return False


def mirror(src: Path, dst: Path, delete: bool = True, verbose: bool = True,
           exclude: tuple[str, ...] = ()) -> dict:
    """把 src 镜像到 dst。

    exclude: 排除的目录/文件名（命中任意一级即排除，且不参与 --delete 判定），
             例如 exclude=("logs",) 可避免运行日志被镜像并造成抖动。
    """
    stats = {"copied": 0, "skipped": 0, "deleted": 0, "bytes": 0, "errors": 0}
    if not src.is_dir():
        raise NotADirectoryError(src)
    dst.mkdir(parents=True, exist_ok=True)

    src_files: set[str] = set()
    for root, _dirs, fns in os.walk(src):
        rel_root = Path(root).relative_to(src)
        # 排除目录：从遍历中剪枝
        if rel_root.parts and _excluded(rel_root, exclude):
            _dirs[:] = []
            continue
        for fn in fns:
            sp = Path(root) / fn
            rel = rel_root / fn
            if _excluded(rel, exclude):
                continue
            src_files.add(str(rel))
            dp = dst / rel
            try:
                st = sp.stat()
                need = True
                if dp.exists():
                    dt = dp.stat()
                    # 同 size 且同 mtime → 跳过
                    if dt.st_size == st.st_size and abs(dt.st_mtime - st.st_mtime) < 1:
                        need = False
                if need:
                    dp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sp, dp)          # copy2 保留 mtime
                    # 校验大小
                    if dp.stat().st_size != st.st_size:
                        raise IOError(f"size mismatch: {rel}")
                    stats["copied"] += 1
                    stats["bytes"] += st.st_size
                    if verbose:
                        print(f"  [复制] {rel} ({st.st_size} B)")
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"] += 1
                print(f"  [错误] {rel}: {e}", file=sys.stderr)

    # 删除冷端多余文件（严格镜像）；被排除项一律不删（它们本就不该被镜像）
    if delete:
        for root, _dirs, fns in os.walk(dst):
            rel_root = Path(root).relative_to(dst)
            if rel_root.parts and _excluded(rel_root, exclude):
                _dirs[:] = []
                continue
            for fn in fns:
                rel = rel_root / fn
                if _excluded(rel, exclude):
                    continue
                if str(rel) not in src_files:
                    try:
                        (dst / rel).unlink()
                        stats["deleted"] += 1
                        if verbose:
                            print(f"  [删除] {rel}")
                    except Exception as e:
                        stats["errors"] += 1
                        print(f"  [删除失败] {rel}: {e}", file=sys.stderr)

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="WeChat Vault 冷备镜像")
    ap.add_argument("--src", required=True, help="热数据目录")
    ap.add_argument("--dst", required=True, help="冷存储目录")
    ap.add_argument("--no-delete", action="store_true", help="不删除冷端多余文件")
    ap.add_argument("--exclude", action="append", default=[],
                    help="排除的目录/文件名（可重复，如 --exclude logs）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    print(f"冷备镜像: {src} → {dst}")
    if args.exclude:
        print(f"  排除: {', '.join(args.exclude)}")
    try:
        st = mirror(src, dst, delete=not args.no_delete, verbose=not args.quiet,
                    exclude=tuple(args.exclude))
    except Exception as e:
        print(f"镜像失败: {e}", file=sys.stderr)
        return 1
    print(f"完成：复制 {st['copied']} / 跳过 {st['skipped']} / 删除 {st['deleted']} "
          f"/ 错误 {st['errors']}（{st['bytes']} 字节）")
    return 0 if st["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
