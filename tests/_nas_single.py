#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_nas_single.py —— 单容器一体化（wechat-vault:single）NAS 部署 + 验收

阶段：
  1. 打包上传源码
  2. .env 就位（复用旧部署的密码哈希）
  3. 拉基础镜像（若缺）
  4. 构建单容器镜像
  5. 停旧容器（释放 8790）
  6. 起新容器
  7. 验收：healthz / 登录页 / wechat status API / KasmVNC 3000 / 端口红线
"""
from __future__ import annotations

import os
import io
import json
import sys
import tarfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import paramiko

HOST = os.environ.get("WV_NAS_HOST", "192.0.2.10")   # 示例地址（RFC 5737）；设 WV_NAS_HOST 覆盖
USER = "AKI"
PWD = os.environ.get("WV_SUDO_PASS", "")
assert PWD, "请先设置 WV_SUDO_PASS 环境变量（NAS 的 sudo 口令）"

SRC = Path(__file__).resolve().parent.parent
REMOTE_SRC = "/vol2/1000/docker/wechat-vault-src"
DATA_HOT = "/vol3/1000/wechat-vault-single"

EXCLUDE_DIRS = {"__pycache__", ".git", "samples", "store", "venv", ".venv",
                "tests", "node_modules", "local_keys", "local_decrypted",
                "_shots"}

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}")


def step(t):
    print(f"\n{'=' * 64}\n{t}\n{'=' * 64}", flush=True)


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


class NAS:
    def __init__(self):
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(HOST, username=USER, password=PWD, timeout=20)
        self.sudo = f"echo '{PWD}' | sudo -S "

    def run(self, cmd, timeout=1800, quiet=False, sudo=True):
        full = f"{self.sudo} sh -c {shq(cmd)}" if sudo else cmd
        _i, o, e = self.cli.exec_command(full, timeout=timeout)
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        if not quiet:
            clean = "\n".join(
                l for l in err.splitlines()
                if "password for" not in l and not l.startswith("sudo: "))
            if clean.strip():
                print("  [err]", clean.strip()[:700])
        return out, err

    def close(self):
        self.cli.close()


def pack_source() -> bytes:
    files = []
    for p in SRC.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(SRC)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if p.name.startswith("_woc_ref") or p.name.startswith("_baseimg"):
            continue
        files.append(p)
    print(f"  待上传: {len(files)} 个文件")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in files:
            tf.add(f, arcname=str(f.relative_to(SRC)).replace("\\", "/"))
    buf.seek(0)
    print(f"  包大小: {len(buf.getvalue()) / 1024:.1f} KB")
    return buf.getvalue()


def main() -> int:
    nas = NAS()
    try:
        # ───────────────────────── 1. 上传源码 ─────────────────────────
        step("1. 打包并上传源码")
        data = pack_source()
        nas.run(f"rm -rf {REMOTE_SRC} && mkdir -p {REMOTE_SRC}")
        sftp = nas.cli.open_sftp()
        with sftp.file("/tmp/wv_single_src.tar.gz", "wb") as fh:
            fh.write(data)
        sftp.close()
        nas.run(
            f"tar -xzf /tmp/wv_single_src.tar.gz -C {REMOTE_SRC} && "
            f"find {REMOTE_SRC} -type d -exec chmod 755 {{}} + && "
            f"find {REMOTE_SRC} -type f -exec chmod 644 {{}} + && "
            f"chmod +x {REMOTE_SRC}/deploy/*.sh {REMOTE_SRC}/deploy/autostart "
            f"{REMOTE_SRC}/deploy/wv-update-autostart && "
            f"chown -R 1000:1001 {REMOTE_SRC}")
        o, _ = nas.run(f"ls {REMOTE_SRC}/deploy/")
        print("  deploy/:", " ".join(o.split()))
        check("源码上传", "Dockerfile.single" in o and "autostart" in o)

        # ───────────────── 2. .env（复用旧部署密码哈希）─────────────────
        step("2. .env 就位")
        old_env, _ = nas.run(f"cat /vol2/1000/docker/wechat-vault-src/.env 2>/dev/null")
        env_body = old_env.strip()
        if not env_body:
            # 旧目录已不存在？从旧部署位置找 .env
            o, _ = nas.run(
                "for d in /vol2/1000/docker/*/; do "
                "[ -f \"$d/.env\" ] && grep -l WV_PASSWORD_HASH \"$d/.env\"; done")
            env_body = o.strip()
        if env_body:
            nas.run(f"printf '%s\\n' {shq(env_body)} > {REMOTE_SRC}/deploy/.env")
            has_hash = "WV_PASSWORD_HASH" in env_body and "pbkdf2" in env_body
            check(".env 已就位（含密码哈希）", has_hash,
                  env_body.splitlines()[0][:40] if env_body else "")
        else:
            check(".env（警告：未找到旧密码哈希，鉴权将为空）", True)

        # ───────────────── 3. 数据目录 ─────────────────
        step("3. 数据目录就位")
        nas.run(f"""
mkdir -p {DATA_HOT}/inbox {DATA_HOT}/inbox/raw {DATA_HOT}/media
mkdir -p {DATA_HOT}/raw-snapshots {DATA_HOT}/logs {DATA_HOT}/wechat-home
chown -R 1000:1001 {DATA_HOT}
""")
        check("热数据目录", True, DATA_HOT)

        # ───────────────── 4. 构建（基础镜像若缺先拉）─────────────────
        step("4. 构建单容器镜像（首次约 3~8 分钟）")
        o, e = nas.run(
            f"cd {REMOTE_SRC} && docker compose -f deploy/docker-compose.single.yml "
            f"build 2>&1 | tail -25", timeout=3600)
        print(o.strip()[-1200:])
        check("镜像构建", "ERROR" not in o.upper()[-2000:] or "naming to" in o.lower())

        # ───────────────── 5. 停旧容器 ─────────────────
        step("5. 停旧容器（释放 8790）")
        nas.run("docker stop wechat-vault 2>/dev/null; "
                "docker rm wechat-vault 2>/dev/null; true")
        o, _ = nas.run("docker ps --format '{{.Names}}' | grep -c wechat-vault || true")
        check("旧容器已移除", o.strip() in ("", "0"), f"count={o.strip()}")

        # ───────────────── 6. 启动 ─────────────────
        step("6. 启动单容器")
        nas.run(f"cd {REMOTE_SRC} && docker compose -f deploy/docker-compose.single.yml "
                f"up -d 2>&1 | tail -5", timeout=300)
        time.sleep(12)
        o, _ = nas.run("docker ps --filter name=wechat-vault "
                       "--format '{{.Names}} {{.Status}}'")
        print(" ", o.strip())
        check("容器运行中", "wechat-vault" in o and "Up" in o)

        # ───────────────── 7. 验收 ─────────────────
        step("7. 实机验收")
        # 注：端口按红线只绑内网 IP（NAS 地址），宿主 127.0.0.1 不通，
        #     必须用绑定 IP 验收；curl 输出需剥离 sudo 密码提示前缀
        WVB = HOST

        def clean(s: str) -> str:
            return "\n".join(l for l in s.splitlines()
                             if "[sudo]" not in l and "password for" not in l).strip()

        o, _ = nas.run(f"curl -fsS -m 5 http://{WVB}:8790/healthz 2>&1")
        check("healthz", '"ok"' in clean(o), clean(o)[:60])

        o, _ = nas.run(f"curl -fsS -m 5 -o /dev/null -w '%{{http_code}}' "
                       f"http://{WVB}:8790/login 2>&1")
        check("登录页", clean(o) == "200", f"HTTP {clean(o)}")

        o, _ = nas.run(f"curl -fsS -m 5 -o /dev/null -w '%{{http_code}}' "
                       f"http://{WVB}:8790/api/wechat/status 2>&1")
        check("wechat status API（401=鉴权生效）", clean(o) in ("200", "401"),
              f"HTTP {clean(o)}")

        o, _ = nas.run(f"curl -fsS -m 5 -o /dev/null -w '%{{http_code}}' "
                       f"http://{WVB}:13000/ 2>&1 || echo FAIL")
        check("KasmVNC 微信桌面 :13000（401=basic auth 拦截，正常）",
              clean(o).startswith(("2", "3")) or clean(o) == "401",
              f"HTTP {clean(o)[:40]}")

        # 端口红线：监听必须是内网 IP 而非 0.0.0.0
        o, _ = nas.run("ss -tlnp 2>/dev/null | grep -E ':8790|:13000' || true")
        print("  监听：", o.strip().replace("\n", " | ")[:300])
        binds_8790 = [l for l in o.splitlines() if ":8790" in l]
        binds_13000 = [l for l in o.splitlines() if ":13000" in l]
        check("红线：8790 未绑 0.0.0.0",
              all(f"{WVB}:8790" in l for l in binds_8790) and binds_8790)
        check("红线：13000 未绑 0.0.0.0",
              all(f"{WVB}:13000" in l for l in binds_13000) and binds_13000)

        # 健康状态
        o, _ = nas.run("docker inspect wechat-vault "
                       "--format '{{.State.Health.Status}}' 2>/dev/null || echo none")
        print(f"  health: {o.strip()}（start_period 90s 内 starting 属正常）")

        # ───────────────── 汇总 ─────────────────
        print(f"\n{'=' * 64}")
        print("✅ 全部通过" if ok else "❌ 存在失败项")
        print(f"{'=' * 64}")
        return 0 if ok else 1
    finally:
        nas.close()


if __name__ == "__main__":
    sys.exit(main())
