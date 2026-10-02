"""安全（认证/限流/网段） + 导出（HTML/CSV/TXT/清单） + PWA 端到端测试。

全部自建临时数据，跑完自动清理。
"""
import csv
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ⚠ 关键：清掉系统代理。否则对 127.0.0.1 的请求会被代理拦截，
#   表现为莫名其妙的 500/502，极易误判成服务端 bug。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

_NO_PROXY = urllib.request.ProxyHandler({})

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# ── 认证模块单元测试（不依赖服务） ──
sys.path.insert(0, str(ROOT / "viewer"))
import wv_auth as A  # noqa: E402

ok = True


def check(label, cond, detail=""):
    global ok
    if not cond:
        ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}")


print("=" * 62)
print("A. 认证模块单元测试")
print("=" * 62)

h = A.hash_password("hunter2")
check("哈希格式正确", h.startswith("pbkdf2_sha256$600000$"), h[:38] + "…")
check("正确密码校验通过", A.verify_password("hunter2", h))
check("错误密码被拒", not A.verify_password("hunter3", h))
check("空密码被拒", not A.verify_password("", h))
check("损坏哈希不炸", not A.verify_password("x", "garbage"))
check("两次哈希盐不同", A.hash_password("a") != A.hash_password("a"))

sec = A.gen_secret()
tok = A.make_token(sec)
check("令牌校验通过", A.check_token(sec, tok))
check("篡改令牌被拒", not A.check_token(sec, tok[:-4] + "AAAA"))
check("换密钥被拒", not A.check_token(A.gen_secret(), tok))
check("空令牌被拒", not A.check_token(sec, ""))
expired = A.make_token(sec, ttl=-10)
check("过期令牌被拒", not A.check_token(sec, expired))

check("网段：命中", A.ip_in_any("192.0.2.88", ["192.0.2.0/24"]))
check("网段：不命中", not A.ip_in_any("10.0.0.5", ["192.0.2.0/24"]))  # preflight-allow：通用私有 IP，用于验证不命中分支
check("网段：多段命中", A.ip_in_any("198.51.100.1", ["192.0.2.0/24", "198.51.100.0/24"]))
check("网段：空白名单=放行", A.ip_in_any("8.8.8.8", []))
check("网段：单 IP 形式", A.ip_in_any("192.0.2.88", ["192.0.2.88"]))
check("限流：5 次后锁定", (lambda t: (
    [t.record_fail("1.2.3.4") for _ in range(5)] and t.is_locked("1.2.3.4")[0]))(
    A.LoginThrottle()))

# ── 起服务测试 ──
print()
print("=" * 62)
print("B. 服务端集成测试（认证 + 导出 + PWA）")
print("=" * 62)

work = Path(tempfile.mkdtemp(prefix="wv_sec_"))
data = work / "data"
inbox = data / "inbox"
for acct, word in [("甲号", "苹果"), ("乙号", "香蕉")]:
    d = inbox / acct
    d.mkdir(parents=True, exist_ok=True)
    lines = ["家庭群"]
    for i in range(1, 7):
        lines.append(f"2026-09-0{i} 09:0{i}:00 {'妈妈' if i % 2 else '我'}")
        lines.append(f"{word}消息第{i}条，含引号\"和逗号,测试转义")
    (d / "家庭群.txt").write_text("\n".join(lines), encoding="utf-8")

PW = "s3cret-pass"
srv_env = dict(os.environ)
srv_env.update({
    "WV_PASSWORD_HASH": A.hash_password(PW),
    "WV_SECRET": A.gen_secret(),
    # 本机测试走 127.0.0.1，白名单须包含它
    "WV_ALLOW_CIDRS": "127.0.0.0/8",
})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"

subprocess.run([PY, str(ROOT / "archiver" / "wv_archiver.py"), "scan",
                "--source", str(inbox), "--store", str(data),
                "--exclude-dir", "raw"],
               capture_output=True, text=True, encoding="utf-8", errors="replace")

proc = subprocess.Popen(
    [PY, str(ROOT / "viewer" / "wv_server.py"),
     "--store", str(data), "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=str(ROOT / "viewer"), env=srv_env,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding="utf-8", errors="replace")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随 302，便于断言跳转目标。"""
    def redirect_request(self, *a, **k):
        return None


_OP_FOLLOW = urllib.request.build_opener(_NO_PROXY)
_OP_NOFOLLOW = urllib.request.build_opener(_NO_PROXY, NoRedirect())


def req(path, method="GET", data=None, cookie=None, follow=True, raw=False):
    """返回 (status, body, headers)。headers 保留原始头（可读 Set-Cookie）。"""
    url = BASE + path
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    r = urllib.request.Request(url, data=body, method=method)
    if isinstance(data, dict):
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    op = _OP_FOLLOW if follow else _OP_NOFOLLOW
    try:
        with op.open(r, timeout=25) as resp:
            b = resp.read()
            return (resp.status, (b if raw else b.decode("utf-8", "replace")),
                    resp.headers)
    except urllib.error.HTTPError as e:
        b = e.read()
        return (e.code, (b if raw else b.decode("utf-8", "replace")), e.headers)


try:
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/login", timeout=3)
            break
        except Exception:
            time.sleep(0.4)

    # 1. 未登录：首页跳登录
    st, body, hd = req("/", follow=False)
    check("未登录访问首页 → 302 跳登录", st == 302 and "/login" in hd.get("Location", ""),
          f"{st} {hd.get('Location','')}")

    # 2. 未登录：API 返回 401
    st, body, _ = req("/api/status")
    check("未登录访问 API → 401", st == 401, f"{st}")
    check("401 带 auth 标记", "auth" in body, body[:80])

    # 3. 登录页可匿名访问
    st, body, _ = req("/login")
    check("登录页匿名可访问", st == 200 and "微信数据方舟" in body, f"{st}")

    # 3b. 健康检查：必须匿名可达（Docker healthcheck 依赖，且源 IP 是容器网段）
    st, body, _ = req("/healthz")
    check("健康检查匿名可达", st == 200 and "ok" in body, f"{st} {body[:40]}")

    # 4. 错误密码
    st, body, _ = req("/api/login", "POST", {"password": "wrong"})
    check("错误密码 → 401", st == 401, f"{st} {body[:60]}")

    # 5. 正确密码 → 下发 Cookie
    st, body, hd = req("/api/login", "POST", {"password": PW})
    sc = hd.get("Set-Cookie", "")
    check("正确密码 → 200", st == 200, f"{st}")
    check("下发了会话 Cookie", "wv_session=" in sc, sc[:80])
    check("Cookie 带 HttpOnly", "HttpOnly" in sc or "httponly" in sc.lower(), sc[:120])
    cookie = sc.split(";")[0]

    # 6. 带 Cookie 可访问
    st, body, _ = req("/api/status", cookie=cookie)
    check("带 Cookie 访问 API → 200", st == 200, f"{st}")
    st, body, _ = req("/", cookie=cookie, follow=False)
    check("带 Cookie 访问首页 → 200", st == 200, f"{st}")

    # 7. 伪造 Cookie 被拒
    st, body, _ = req("/api/status", cookie="wv_session=forged.token.here.x")
    check("伪造 Cookie → 401", st == 401, f"{st}")

    # 8. 导出：单会话 HTML
    st, body, hd = req("/api/conversations?limit=10", cookie=cookie)
    convs = json.loads(body)["items"]
    check("会话列表可读", len(convs) == 2, f"{len(convs)}")
    cid = convs[0]["conv_id"]

    st, body, hd = req(f"/api/export/conv?conv_id={urllib.parse.quote(cid)}&fmt=html",
                       cookie=cookie)
    check("导出 HTML → 200", st == 200, f"{st}")
    check("HTML 含 doctype", body.lstrip().startswith("<!DOCTYPE"), body[:30])
    check("HTML 含会话标题", convs[0]["title"] in body)
    check("HTML 含消息正文", "消息第1条" in body)
    check("HTML 已转义引号", "&quot;" in body or "&#34;" in body or '"' not in body.split("消息第1条")[0][-40:])
    check("下载头是附件名", "filename*=UTF-8" in hd.get("Content-Disposition", ""),
          hd.get("Content-Disposition", "")[:70])

    # 9. 导出 CSV（Excel 兼容：BOM + 正确转义）
    st, body, hd = req(f"/api/export/conv?conv_id={urllib.parse.quote(cid)}&fmt=csv",
                       cookie=cookie, raw=True)
    check("导出 CSV → 200", st == 200, f"{st}")
    check("CSV 带 UTF-8 BOM", body[:3] == b"\xef\xbb\xbf", repr(body[:6]))
    text = body.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    check("CSV 表头正确", rows[0][:4] == ["时间", "发送者", "是否我发出", "类型"], rows[0][:4])
    check("CSV 行数 = 1 表头 + 6 消息", len(rows) == 7, f"{len(rows)}")
    check("CSV 含逗号字段未错列", any("逗号" in r[4] for r in rows[1:]), "")

    # 10. 导出 TXT
    st, body, _ = req(f"/api/export/conv?conv_id={urllib.parse.quote(cid)}&fmt=txt",
                      cookie=cookie)
    check("导出 TXT → 200", st == 200, f"{st}")
    check("TXT 含日期分隔", "———" in body, "")
    check("TXT 含发送者", "妈妈" in body or "我:" in body)

    # 11. 导出全部（按账号）
    st, body, hd = req("/api/export/all?fmt=csv", cookie=cookie, raw=True)
    check("导出全部 CSV → 200", st == 200, f"{st}")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    check("全部导出含账号列", rows[0][0] == "账号", rows[0][:3])
    check("全部导出行数 = 1 + 12", len(rows) == 13, f"{len(rows)}")

    st, body, _ = req("/api/export/all?account=%E7%94%B2%E5%8F%B7&fmt=csv",
                      cookie=cookie, raw=True)
    rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    check("按账号导出只含该账号", all(r[0] == "甲号" for r in rows[1:]), f"{len(rows)-1} 行")

    st, body, _ = req("/api/export/all?fmt=html", cookie=cookie)
    check("导出全部 HTML → 200", st == 200 and body.count("<!DOCTYPE") == 1, f"{st}")

    # 12. 会话清单 JSON
    st, body, _ = req("/api/export/manifest", cookie=cookie)
    mf = json.loads(body)
    check("清单 JSON 可解析", mf["total_convs"] == 2, f"{mf.get('total_convs')}")
    check("清单含账号列表", sorted(mf["accounts"]) == ["乙号", "甲号"], str(mf["accounts"]))
    check("清单消息总数 = 12", mf["total_msgs"] == 12, f"{mf.get('total_msgs')}")

    # 13. 无 Cookie 时导出也被拦
    st, body, _ = req("/api/export/all?fmt=csv")
    check("未登录导出被拦 → 401", st == 401, f"{st}")

    # 14. PWA 资源
    st, body, hd = req("/manifest.webmanifest", cookie=cookie)
    m = json.loads(body)
    check("manifest 可解析", m["name"].startswith("微信数据方舟"), m.get("name"))
    check("manifest 有 display standalone", m["display"] == "standalone")
    check("manifest 有图标", len(m.get("icons", [])) >= 1)

    st, body, hd = req("/sw.js", cookie=cookie)
    check("sw.js 可访问", st == 200 and "addEventListener" in body, f"{st}")
    check("sw.js 不缓存", "no-cache" in (hd.get("Cache-Control") or ""),
          hd.get("Cache-Control", "")[:50])

    st, body, _ = req("/icon.svg", cookie=cookie)
    check("icon.svg 可访问", st == 200 and "<svg" in body, f"{st}")

    st, body, _ = req("/favicon.ico", cookie=cookie)
    check("favicon 可访问", st == 200, f"{st}")

    # 15. 未登录时 PWA 资源也要能拿（PWA 安装需要）
    st, body, _ = req("/manifest.webmanifest")
    check("manifest 匿名可访问", st == 200, f"{st}")

    # 16. 首页含新元素
    st, body, _ = req("/", cookie=cookie)
    check("首页含导出按钮", 'id="btnExportAll"' in body)
    check("首页含登出按钮", 'id="btnLogout"' in body)
    check("首页含导出面板", 'id="exportPanel"' in body)
    check("首页含 manifest 链接", 'manifest.webmanifest' in body)
    check("首页注册 SW", "serviceWorker" in body)

    # 17. 登出
    # 说明：本项目用「无状态签名 Cookie」，服务端不存 session。
    #   delete_cookie 只下发过期 Cookie 让浏览器丢弃；旧 token 在有效期内
    #   仍能通过验签 —— 这是无状态方案的固有取舍（换来零 session 存储）。
    #   所以这里断言「下发了清除指令」+「伪造 token 被拒」，而不是「旧 token 立刻失效」。
    st, body, hd = req("/api/logout", "POST", cookie=cookie)
    check("登出 → 200", st == 200, f"{st}")
    sc = hd.get("Set-Cookie") or ""
    check("登出下发清除 Cookie",
          "wv_session=" in sc and ("Max-Age=0" in sc or "expires=" in sc.lower()
                                   or 'wv_session=""' in sc),
          sc[:90])
    st, body, _ = req("/api/status", cookie="wv_session=forged.token.here.x")
    check("伪造 Cookie 被拒（验签生效）", st == 401, f"{st}")

finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
    # 打印服务日志尾部（排错用）
    try:
        out = proc.stdout.read() if proc.stdout else ""
        if out and "Traceback" in out:
            print("\n[服务日志含异常]")
            print(out[-1200:])
    except Exception:
        pass

print()
print("=" * 62)
print("结果:", "全部通过" if ok else "有失败项")
print("=" * 62)
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
