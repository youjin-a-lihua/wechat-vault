"""NAS 全量部署 + 实机验收（安全 / 导出 / PWA / 双轨 / 多账号）。

用法：
    python _nas_full.py push      # 上传源码 + 重建容器
    python _nas_full.py verify    # 只跑实机验收（假定容器已在跑）
    python _nas_full.py all       # 两步都做（默认）

前置：本地已有 NAS 凭据（见文件内常量）。
"""
import os
import io
import json
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import paramiko

HOST = os.environ.get("WV_NAS_HOST", "192.0.2.10")   # 示例地址（RFC 5737）；设 WV_NAS_HOST 覆盖
USER = "AKI"
PWD = os.environ.get("WV_SUDO_PASS", "")
assert PWD, "请先设置 WV_SUDO_PASS 环境变量（NAS 的 sudo 口令）"
PORT = 8790
BASE = f"http://{HOST}:{PORT}"

# NAS 本机访问（走 SSH 内部 curl，用于白名单内的自测）
LOCAL_BASE = f"http://127.0.0.1:{PORT}"

SRC = Path(__file__).resolve().parent.parent
REMOTE_SRC = "/vol2/1000/docker/wechat-vault-src"
HOT = "/vol3/1000/wechat-vault"
COLD = "/vol00/HSH721414ALN6M0/wechat-vault-cold"

# 本次实机验收使用的密码（仅用于验证；之后可用 wv_auth.py 生成正式哈希）
TEST_PW = "wv-nas-check-2026"

EXCLUDE_DIRS = {"__pycache__", ".git", "samples", "store", "venv", ".venv",
                "tests", "node_modules"}

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}")


def step(t):
    print(f"\n{'='*64}\n{t}\n{'='*64}")


class NAS:
    def __init__(self):
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect(HOST, username=USER, password=PWD, timeout=20)
        self.sudo = f"echo '{PWD}' | sudo -S "

    def run(self, cmd, timeout=600, quiet=False, sudo=True):
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


def shq(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随 302，便于断言跳转目标。"""
    def redirect_request(self, *a, **k):
        return None


_OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_OP_NOFOLLOW = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect())


def http(path, cookie=None, timeout=15, data=None, method="GET", follow=True):
    """访问 NAS 上的服务（从本机直连，需在 WV_ALLOW_CIDRS 内）。

    follow=False 时不跟随重定向 —— 断言 302 目标时必须用这个，
    否则 urllib 会自动跟到 /login 返回 200，把正确的行为误判为失败。
    """
    url = BASE + path
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    r = urllib.request.Request(url, data=body, method=method)
    if isinstance(data, dict):
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    op = _OP if follow else _OP_NOFOLLOW
    try:
        with op.open(r, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers
    except Exception as e:
        return 0, str(e).encode(), {}


# ─────────────────────────────────────────────────────────────
# push
# ─────────────────────────────────────────────────────────────
def push(nas: NAS):
    step("1. 打包本地源码")
    files = []
    for p in SRC.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(SRC)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        files.append(p)
    print(f"  待上传: {len(files)} 个文件")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for f in files:
            tf.add(f, arcname=str(f.relative_to(SRC)).replace("\\", "/"))
    buf.seek(0)
    print(f"  包大小: {len(buf.getvalue())/1024:.1f} KB")

    step("2. 上传并解包")
    nas.run(f"rm -rf {REMOTE_SRC} && mkdir -p {REMOTE_SRC}")
    sftp = nas.cli.open_sftp()
    with sftp.file("/tmp/wv_src.tar.gz", "wb") as fh:
        fh.write(buf.getvalue())
    sftp.close()
    o, _ = nas.run(f"tar -xzf /tmp/wv_src.tar.gz -C {REMOTE_SRC} && "
                   f"find {REMOTE_SRC} -type d -exec chmod 755 {{}} + && "
                   f"find {REMOTE_SRC} -type f -exec chmod 644 {{}} + && "
                   f"chmod +x {REMOTE_SRC}/deploy/*.sh && "
                   f"chown -R 1000:1001 {REMOTE_SRC} && "
                   f"ls {REMOTE_SRC}")
    print(o.strip()[:900])

    step("3. 目录结构就位（热侧 / 冷侧）")
    nas.run(f"""
mkdir -p {HOT}/inbox {HOT}/inbox/raw {HOT}/media {HOT}/raw-snapshots {HOT}/logs
mkdir -p {COLD}
chown -R 1000:1001 {HOT} {COLD}
""")
    o, _ = nas.run(f"ls -la {HOT}/")
    print(o.strip()[:1000])

    step("4. 生成正式访问密码哈希")
    admin_pw = "AKI-WeChat-Vault"
    o, _ = nas.run(
        f"cd {REMOTE_SRC}/viewer && "
        f"python3 wv_auth.py {shq(admin_pw)} 2>&1 | tail -5",
        quiet=True)
    print(o.strip()[-600:])
    lines = [l.strip() for l in o.strip().splitlines() if l.startswith("pbkdf2_sha256$")]
    pw_hash = lines[-1] if lines else ""
    if not pw_hash:
        print("  !! 未拿到哈希，改用容器内生成")
    else:
        print(f"  哈希前缀: {pw_hash[:46]}…")
        Path(Path(__file__).parent / "_nas_pwhash.txt").write_text(
            pw_hash, encoding="utf-8")
        print("  已写入 tests/_nas_pwhash.txt（供 compose 使用）")

    # 写入 .env（compose 读取）
    secret = nas.run(
        f"cd {REMOTE_SRC}/viewer && python3 wv_auth.py --secret 2>&1 | tail -2",
        quiet=True)[0].strip().splitlines()[-1].strip()
    # ⚠ 关键坑：docker compose 会解析 .env 里的 `$xxx` 做变量插值。
    #   PBKDF2 哈希形如 pbkdf2_sha256$600000$<salt>$<hash>，含 4 个 `$`，
    #   若原样写入，compose 会把 `$600000`、`$Ha/...` 当变量名吃掉 →
    #   容器里拿到的哈希被静默篡改 → 密码永远验证失败（且日志看不出异常）。
    #   解法：`$` 写成 `$$`，compose 会还原成单个 `$`。
    def esc(v: str) -> str:
        return v.replace("$", "$$")

    env_body = (
        f"WV_PASSWORD_HASH={esc(pw_hash)}\n"
        f"WV_SECRET={esc(secret)}\n"
        f"WV_ALLOW_CIDRS=192.168.31.0/24,192.168.100.0/24,127.0.0.1/32\n"
    )
    nas.run(f"cat > {REMOTE_SRC}/deploy/.env <<'EOF'\n{env_body}EOF\n"
            f"chmod 600 {REMOTE_SRC}/deploy/.env && "
            f"chown 1000:1001 {REMOTE_SRC}/deploy/.env")
    o, _ = nas.run(f"cat {REMOTE_SRC}/deploy/.env")
    print("  .env:\n   " + o.strip().replace("\n", "\n   ")[:400])

    step("5. 构建镜像")
    nas.run(f"cd {REMOTE_SRC}/deploy && docker compose down --remove-orphans 2>&1 | tail -4")
    o, _ = nas.run(f"cd {REMOTE_SRC}/deploy && docker compose build 2>&1 | tail -30",
                   timeout=2400)
    print(o.strip()[-2200:])

    step("6. 启动容器")
    o, _ = nas.run(f"cd {REMOTE_SRC}/deploy && docker compose up -d 2>&1 | tail -12")
    print(o.strip()[:900])

    step("7. 等待 healthy")
    for i in range(40):
        o, _ = nas.run("docker ps --filter name=wechat-vault --format '{{.Status}}'",
                       quiet=True)
        s = o.strip()
        if "healthy" in s:
            print(f"  就绪: {s}")
            break
        if i % 5 == 0:
            print(f"  [{i*3}s] {s or '(暂无)'}")
        time.sleep(3)
    else:
        print("  !! 未在预期时间内 healthy")
    o, _ = nas.run("docker ps -a --filter name=wechat-vault --format '{{.Names}} | {{.Status}} | {{.Ports}}'")
    print(o.strip())
    o, _ = nas.run("docker logs wechat-vault --tail 30 2>&1", quiet=True)
    print("--- 日志 ---")
    print(o.strip()[-1800:])


# ─────────────────────────────────────────────────────────────
# verify
# ─────────────────────────────────────────────────────────────
def verify(nas: NAS, local=True):
    step("A. 容器与端口绑定（红线：不得 0.0.0.0）")
    o, _ = nas.run("docker ps -a --filter name=wechat-vault "
                   "--format '{{.Names}}|{{.Status}}|{{.Ports}}'", quiet=True)
    line = o.strip()
    print("  " + line)
    check("容器 healthy", "healthy" in line, line[:80])
    check("端口未绑 0.0.0.0（红线）", "0.0.0.0:8790" not in line,
          "含 0.0.0.0 则违反红线")

    step("B. 挂载点")
    o, _ = nas.run("docker inspect wechat-vault "
                   "--format '{{range .Mounts}}{{.Source}} -> {{.Destination}} "
                   "({{.Mode}}){{println}}{{end}}'", quiet=True)
    print(o.strip())
    check("热侧已挂载", HOT in o, "")
    check("冷侧已挂载", COLD in o, "")

    step("C. 容器内身份（非 root）")
    o, _ = nas.run("docker exec wechat-vault id 2>&1", quiet=True)
    print("  " + o.strip())
    check("容器内非 root", "uid=1000" in o, o.strip()[:60])

    step("D. 认证：未登录拦截")
    st, body, hd = http("/api/status")
    check("未登录访问 API → 401", st == 401, f"{st}")
    st, body, hd = http("/", follow=False)
    check("未登录访问首页 → 302 /login", st == 302 and "login" in
          (hd.get("Location") or ""), f"{st} {hd.get('Location')}")
    st, body, hd = http("/login")
    check("登录页可访问", st == 200 and "微信数据方舟" in body.decode("utf-8", "replace"),
          f"{st}")

    step("E. 认证：登录 + Cookie")
    st, body, hd = http("/api/login", data={"password": "AKI-WeChat-Vault"},
                        method="POST")
    sc = hd.get("Set-Cookie") or ""
    check("正确密码 → 200", st == 200, f"{st} {body[:50]}")
    check("下发 wv_session", "wv_session=" in sc, sc[:60])
    check("Cookie HttpOnly", "httponly" in sc.lower(), "")
    check("Cookie SameSite", "samesite" in sc.lower(), "")
    cookie = sc.split(";")[0]
    st, body, _ = http("/api/login", data={"password": "definitely-wrong"},
                       method="POST")
    check("错误密码 → 401", st == 401, f"{st}")

    step("F. 登录后：核心 API")
    # ⚠️ 数据相关断言必须区分两种情况：
    #   ① 库里有数据 → 断言"非空"，这是功能正确性
    #   ② 库是空库（demo 已清、真实数据尚未导入）→ 断言"接口可用且返回合法结构"
    #   否则空库会把"接口正常"误报成"功能失败"（2026-10-01 实测踩到）。
    st, body, _ = http("/api/status", cookie=cookie)
    check("status 200", st == 200, f"{st}")
    empty_db = False
    if st == 200:
        j = json.loads(body)
        print("  ", json.dumps(j, ensure_ascii=False)[:300])
        conv_n = j.get("conv_count", 0)
        acct_n = j.get("account_count", 0)
        empty_db = conv_n == 0 and acct_n == 0
        if empty_db:
            print("  ⓘ 当前为空库（demo 已清 / 真实数据未导入）"
                  "，数据类断言改为「接口可用」口径")
        check("conv_count 为合法整数", isinstance(conv_n, int), f"{conv_n}")
        check("account_count 为合法整数", isinstance(acct_n, int), f"{acct_n}")
        if not empty_db:
            check("会话数 > 0", conv_n > 0, f"{conv_n}")
            check("账号数 > 0", acct_n > 0, f"{acct_n}")

    st, body, _ = http("/api/accounts", cookie=cookie)
    if st == 200:
        j = json.loads(body)
        names = [a["account"] for a in j["items"]]
        print("  账号:", names)
        check("accounts 200 且结构合法", "items" in j, str(list(j)[:4]))
        if not empty_db:
            check("账号列表非空", len(names) > 0, str(names))

    st, body, _ = http("/api/archive", cookie=cookie)
    if st == 200:
        j = json.loads(body)
        print("  归档:", json.dumps(j, ensure_ascii=False)[:280])
        check("存档轨已启用", j.get("enabled") is True, "")
        check("archive 200 且含 snapshots 字段", "snapshots" in j, "")
        if not empty_db:
            check("存档轨有快照", len(j.get("snapshots", [])) >= 1, "")

    step("G. 搜索（中文 FTS）")
    # 搜索词不能写死 —— NAS 上是真实数据，得先从库里取一个真实片段再搜，
    # 否则「搜不到」只说明该词不存在，不能说明检索坏了。
    st, body, _ = http("/api/export/manifest", cookie=cookie)
    items = json.loads(body).get("items", []) if st == 200 else []
    # 会话标题一定可被搜到（标题已入 FTS 的 conv_id 维度外，取消息原文更稳）
    probe = ""
    if items:
        cid0 = items[0]["conv_id"]
        st, body, _ = http(
            "/api/messages?conv_id=" + urllib.parse.quote(cid0) + "&limit=5",
            cookie=cookie)
        if st == 200:
            for m in json.loads(body).get("items", []):
                c = (m.get("content") or "").strip()
                if len(c) >= 2:
                    probe = c[:4]
                    break
    print(f"  探测搜索词: {probe!r}")
    if empty_db:
        print("  ⓘ 空库：跳过「取到消息样本」断言，仅验证搜索接口可用")
        check("搜索接口可用（空库）",
              http("/api/search?q=x", cookie=cookie)[0] == 200, "")
    else:
        check("取到消息样本", bool(probe), f"{probe!r}")
    if probe:
        q = urllib.parse.quote(probe)
        st, body, _ = http(f"/api/search?q={q}&limit=5", cookie=cookie)
        check("搜索接口 200", st == 200, f"{st}")
        if st == 200:
            j = json.loads(body)
            check(f"搜索 {probe!r} 命中", j.get("count", 0) > 0,
                  f"命中 {j.get('count')}")
    # 顺便验证「不存在的词」返回 0 而不是报错
    st, body, _ = http("/api/search?q=" + urllib.parse.quote("这个词肯定不存在zzz"),
                       cookie=cookie)
    check("无关词返回 0 且不报错",
          st == 200 and json.loads(body).get("count") == 0, f"{st}")

    step("H. 导出功能")
    st, body, hd = http("/api/conversations?limit=3", cookie=cookie)
    convs = json.loads(body)["items"] if st == 200 else []
    if empty_db:
        print("  ⓘ 空库：跳过「取到会话」断言，仅验证导出接口可用")
        check("conversations 接口可用（空库）", st == 200, f"{st}")
    else:
        check("取到会话", len(convs) > 0, f"{len(convs)}")
    if convs:
        cid = convs[0]["conv_id"]
        q = urllib.parse.quote(cid)
        st, b, hd = http(f"/api/export/conv?conv_id={q}&fmt=html", cookie=cookie)
        check("导出单会话 HTML", st == 200 and b.lstrip().startswith(b"<!DOCTYPE"),
              f"{st} {len(b)}B")
        check("HTML 下载头 RFC5987", "filename*=UTF-8" in
              hd.get("Content-Disposition", ""),
              hd.get("Content-Disposition", "")[:60])
        st, b, hd = http(f"/api/export/conv?conv_id={q}&fmt=csv", cookie=cookie)
        check("导出单会话 CSV", st == 200 and b[:3] == b"\xef\xbb\xbf",
              f"{st} {len(b)}B")
        st, b, hd = http(f"/api/export/conv?conv_id={q}&fmt=txt", cookie=cookie)
        check("导出单会话 TXT", st == 200 and len(b) > 10, f"{st} {len(b)}B")
    st, b, hd = http("/api/export/all?fmt=csv", cookie=cookie)
    if empty_db:
        # 空库时导出全库会返回 404（没有可导内容）—— 这是**正确行为**，
        # 不是缺陷。断言口径改为「要么 200 带 BOM，要么 404 空库说明书」。
        check("导出全库 CSV（空库：200 或 404 均可）",
              st in (200, 404), f"{st} {len(b)}B")
    else:
        check("导出全库 CSV", st == 200 and b[:3] == b"\xef\xbb\xbf",
              f"{st} {len(b)}B")
    st, b, hd = http("/api/export/manifest", cookie=cookie)
    check("导出清单 JSON", st == 200, f"{st}")
    if st == 200:
        mf = json.loads(b)
        print("  清单:", json.dumps(mf, ensure_ascii=False)[:260])

    step("I. PWA")
    st, b, hd = http("/manifest.webmanifest")
    check("manifest 匿名可访问", st == 200, f"{st}")
    if st == 200:
        m = json.loads(b)
        check("manifest standalone", m.get("display") == "standalone", "")
        check("manifest 有图标", len(m.get("icons", [])) >= 1, "")
    st, b, hd = http("/sw.js")
    check("sw.js 可访问", st == 200 and b"addEventListener" in b, f"{st}")
    check("sw.js no-cache", "no-cache" in (hd.get("Cache-Control") or ""),
          hd.get("Cache-Control", "")[:40])
    st, b, hd = http("/icon.svg")
    check("icon.svg 可访问", st == 200 and b"<svg" in b, f"{st}")

    step("J. 页面元素")
    st, b, _ = http("/", cookie=cookie)
    html = b.decode("utf-8", "replace")
    for sel in ['id="acctSwitcher"', 'id="btnArchive"', 'id="btnExportAll"',
                'id="btnLogout"', 'id="exportPanel"', 'manifest.webmanifest',
                "serviceWorker"]:
        check(f"页面含 {sel}", sel in html, "")

    step("K. 静态资源权限（历史 500 根因复检）")
    for p in ["/app.js", "/app.css", "/login.css"]:
        st, b, _ = http(p, cookie=cookie)
        check(f"{p} 200", st == 200 and len(b) > 100, f"{st} {len(b)}B")

    step("L. 登出")
    st, b, hd = http("/api/logout", cookie=cookie, method="POST")
    check("登出 200", st == 200, f"{st}")
    # 说明：本项目用「无状态签名 Cookie」，服务端不存 session 表。
    # delete_cookie 只会下发一个过期 Cookie 让浏览器丢弃；旧 token 在有效期内
    # 仍能通过验签 —— 这是无状态方案的固有取舍（换来的是零 session 存储、
    # 多实例可直接水平扩展）。真正的失效手段是轮换 WV_SECRET。
    # 因此这里断言的是「响应确实下发了清除指令」，而非「旧 token 立即失效」。
    sc = hd.get("Set-Cookie") or ""
    check("登出下发清除 Cookie", ("wv_session=" in sc and
                              ("Max-Age=0" in sc or "expires=" in sc.lower() or
                               "wv_session=;" in sc or 'wv_session=""' in sc)),
          sc[:90])
    # 用「伪造 token」验证服务端确实在验签（而不是来者不拒）
    st2, _, _ = http("/api/status", cookie="wv_session=forged.token.here.x")
    check("伪造 Cookie 被拒（验签生效）", st2 == 401, f"{st2}")
    # 轮换密钥后旧 token 必须失效
    print("  （无状态方案：旧 token 需轮换 WV_SECRET 才彻底失效，属设计取舍）")

    step("M. 端口绑定红线复检（NAS 本机视角）")
    # 端口只绑 NAS 地址，不绑 0.0.0.0 →
    #   预期：宿主 127.0.0.1 连不上；NAS 地址 能连上。
    # 这正是「绝不开公网」的实证。
    o, _ = nas.run(f"curl -s -m 6 -o /dev/null -w '%{{http_code}}' "
                   f"http://127.0.0.1:{PORT}/healthz; echo", quiet=True)
    c1 = o.strip()
    print("  宿主 127.0.0.1 ->", c1 or "(空)")
    check("127.0.0.1 连不上（端口未绑 0.0.0.0，红线达成）",
          c1 in ("000", ""), f"返回 {c1}")

    o, _ = nas.run(f"curl -s -m 6 -o /dev/null -w '%{{http_code}}' "
                   f"http://{HOST}:{PORT}/healthz; echo", quiet=True)
    c2 = o.strip()
    print(f"  宿主 {HOST} ->", c2 or "(空)")
    check("局域网 IP 可连（200）", c2 == "200", f"返回 {c2}")

    o, _ = nas.run(f"ss -ltnp 2>/dev/null | grep {PORT} || "
                   f"netstat -ltnp 2>/dev/null | grep {PORT}", quiet=True)
    listen = o.strip()
    print("  监听:", listen[:200])
    # ⚠ 只检查「本地地址」列。
    #   ss 输出形如：
    #     LISTEN 0 4096  NAS 地址:8790  0.0.0.0:*  users:(("docker-proxy",...))
    #   后面的 0.0.0.0:* 是「对端地址」（远端任意），不是监听地址 ——
    #   直接 grep "0.0.0.0:" 会把正确配置误判为违反红线。
    local_addr = ""
    for ln in listen.splitlines():
        parts = ln.split()
        if "LISTEN" in ln and len(parts) >= 4:
            local_addr = parts[3]
            break
    print("  本地监听地址列:", local_addr)
    check("监听地址是局域网 IP（非 0.0.0.0）",
          local_addr.startswith("NAS 地址:"), local_addr)
    check("未绑定通配地址 0.0.0.0:8790",
          local_addr != f"0.0.0.0:{PORT}" and local_addr != f"[::]:{PORT}",
          local_addr)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    nas = NAS()
    try:
        if mode in ("push", "all"):
            push(nas)
        if mode in ("verify", "all"):
            step("实机验收")
            verify(nas)
    finally:
        nas.close()

    print()
    print("=" * 64)
    print("NAS 实机验收结果:", "全部通过" if ok else "有失败项")
    print("=" * 64)


if __name__ == "__main__":
    main()
