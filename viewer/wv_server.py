#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 查看器后端
=========================

极简 FastAPI 服务：读归档目录 → 解析 → 提供 Web 界面 + API。

设计原则：
  - 零常驻：只在被访问时工作，不做后台轮询
  - 只读：不修改归档数据
  - 局域网：默认只监听 127.0.0.1，可通过环境变量放开
  - 自带解析：依赖同仓库的 wv_parser.py

启动：
  python wv_server.py --data <归档目录> --port 8790
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# 允许直接运行（把 parser 目录加入路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "parser"))
# viewer 自身目录（wv_auth / wv_export 与本文件同目录；
# uvicorn --app-dir /app 模块方式启动时该目录不在 sys.path，会 ModuleNotFoundError）
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
    from fastapi.responses import (HTMLResponse, JSONResponse, FileResponse,
                                   RedirectResponse, Response, PlainTextResponse)
    import uvicorn
except ImportError:
    print("缺少依赖，请先安装：pip install fastapi uvicorn", file=sys.stderr)
    raise

import wv_parser as P  # noqa: E402
import wv_auth as A  # noqa: E402
import wv_export as X  # noqa: E402
import logging

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# FTS5 的 trigram 分词器把文本切成三个字符一组的 token，因此**短于 3 个字符
# 的查询永远命中 0 条**（不报错，只是空结果）。中文最常用的词恰好是两个
# 字：快递 / 会议 / 老师 / 吃饭 …… 实测 189 万条的库上，「快递」LIKE 有
# 1016 条、FTS MATCH 返回 0；「羽毛球」3 个字则正常命中。
# 所以走 FTS 的门槛必须按分词器的最小长度来定，不能凭感觉写 2。
FTS_MIN_CHARS = 3

# 媒体 URL 白名单：只允许「站内相对路径」与 http(s) 绝对地址。
# media / media_full 是 DB 里的文本，正常情况下由 wv_media 回填为 /media/xxx，
# 但库是开放的——用户把别人的 vault.db 拷进来、或旧版解析器写脏了，
# 就可能混入 javascript:/data: 之类。这些值会被直接拼进 <img src> 与 <a href>，
# 前者无害（img src 不执行脚本），后者在导出/归档 HTML 里就是可点的 XSS。
# 白名单只放行两类，其余一律抹成空串（= 退化为占位符，不丢消息）。
import re as _re
_MEDIA_OK = _re.compile(r"^(?:https?://|/(?!/))", _re.I)

def _safe_media(v) -> str:
    """净化 media / media_full：非白名单 scheme 返回空串。"""
    s = (v or "")
    if not isinstance(s, str) or not s:
        return ""
    s = s.strip()
    return s if _MEDIA_OK.match(s) else ""

def _sanitize_rows(rows: list) -> list:
    """出口净化：所有返回给浏览器的消息行都过一遍 scheme 白名单。"""
    for r in rows:
        if isinstance(r, dict):
            if r.get("media"):
                r["media"] = _safe_media(r["media"])
            if r.get("media_full"):
                r["media_full"] = _safe_media(r["media_full"])
    return rows

# 多账号：直接躺在投放口根目录的文件归入此账号（与 archiver 约定一致）
DEFAULT_ACCOUNT = "默认账号"

# 聊天记录按本地自然日切分（与前端 fmtDay 口径一致）。
# 偏移可配：海外部署设 WV_TZ_OFFSET，SQL 里的日期表达式必须跟着一起变，
# 否则「按日期查找」的边界会和实际展示差一整天。
try:
    TZ_OFFSET = float(os.environ.get("WV_TZ_OFFSET", "8"))
except ValueError:
    TZ_OFFSET = 8.0
TZ8 = dt.timezone(dt.timedelta(hours=TZ_OFFSET))
# SQLite 的时区修饰串，供所有 strftime/date 复用（避免各处再写一遍 %+g 格式化）
TZ_SQL = "%+g hours" % TZ_OFFSET
DAY_EXPR = "date(ts, 'unixepoch', '%s')" % TZ_SQL

def _day_bounds(day: str) -> tuple[int, int]:
    """把 `YYYY-MM-DD`（东八区自然日）换算为 [lo, hi) 时间戳；非法输入返回 (0,0)。"""
    try:
        d = dt.datetime.strptime((day or "").strip(), "%Y-%m-%d")
    except ValueError:
        return 0, 0
    lo = int(d.replace(tzinfo=TZ8).timestamp())
    return lo, lo + 86400

def _ts_bounds(date_from: str = "", date_to: str = "") -> tuple[int, int]:
    """起止日期（含首含尾，东八区）→ [lo, hi) 时间戳；空值表示不限。"""
    lo = _day_bounds(date_from)[0] if date_from else 0
    hi = _day_bounds(date_to)[1] if date_to else 0
    return lo, hi

STATE: dict = {
    "data_dir": None,
    "index_path": None,
    "convs": [],
    "loaded_at": 0,
    "loading": False,
}
_lock = threading.Lock()

# 整库级重活的互斥（/api/ingest 与 /api/reload 共用一把）
#
# 为什么要锁：/api/ingest 会连着跑「归档 → 媒体链路 → 重载」，动辄几分钟，
# 全程在写同一份 vault.db；/api/reload 会重建内存索引。两者并发的后果是
# 后一个覆盖前一个的缓存，或者重载读到一个写了一半的库 —— 前端表现为
# 「消息数对不上、刚归档的图不显示」，而且很难复现。
#
# 两种取锁策略（刻意不同）：
#   · ingest 非阻塞 —— 同一时刻只该有一个归档在跑。第二个直接 409，
#     让它排队只会使客户端干等几分钟，不如明确回一句"已经在跑了"。
#   · reload 带超时阻塞 —— 它自己很快，只有撞上归档才需要等；
#     给 20 秒耐心，等不到再报错（autosync 下一轮会自己重载，不会永久丢）。
_HEAVY_LOCK = threading.Lock()
_HEAVY_LOCK_TIMEOUT = 20

def _setup_logging() -> None:
    """统一日志配置：级别取 WV_LOG_LEVEL（默认 INFO），已配置则不覆盖。

    为什么需要它：本项目原先 153 处 print、0 处 logging —— 无法分级、无法过滤、
    无法接入告警。改用 logging 后，默认行为是 WARNING 以上打到 stderr，
    但 info/debug 需要显式配置级别，故在此统一初始化。
    """
    import logging as _lg
    from logging.handlers import RotatingFileHandler
    if _lg.getLogger().handlers:
        return
    lvl = (os.environ.get("WV_LOG_LEVEL") or "INFO").upper()
    fmt = _lg.Formatter("%(asctime)s %(levelname).1s %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    root = _lg.getLogger()
    root.setLevel(getattr(_lg, lvl, _lg.INFO))
    sh = _lg.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # 可选文件日志：容器日志靠 docker 的 max-size 兜底，但 autosync 那一侧
    # （s6 长跑服务）stdout 会被 s6 收走，查问题时常常只剩半截。
    # 设 WV_LOG_FILE 就同时落一份带轮转的，满了自动切，不会把数据盘撑爆。
    lf = os.environ.get("WV_LOG_FILE", "").strip()
    if lf:
        try:
            mb = float(os.environ.get("WV_LOG_MAX_MB", "20"))
            keep = int(os.environ.get("WV_LOG_KEEP", "3"))
            fh = RotatingFileHandler(lf, maxBytes=int(mb * 1024 * 1024),
                                     backupCount=max(1, keep), encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            # 日志目录不可写不该让服务起不来
            print("无法开启文件日志（%s）：%s" % (lf, e), file=sys.stderr)

_setup_logging()

app = FastAPI(title="WeChat Vault", docs_url=None, redoc_url=None)

# 访问控制（项目红线：不开公网）
#   未设 WV_PASSWORD_HASH → 认证关闭（本机/可信网络零摩擦）
#   设了密码 → 除白名单外全部要求登录
# 认证持久化文件：首次在网页上设置的管理员密码写在这里（随数据卷持久，重启不丢）。
# 优先级：环境变量 WV_PASSWORD_HASH / WV_SECRET > auth.json > 未初始化。
AUTH_FILE = Path(
    os.environ.get("WV_AUTH_FILE", "")
    or os.path.join(os.environ.get("WV_STORE", "/data"), "auth.json")
)

def _load_file_auth() -> dict:
    """读 auth.json（Web 设置页写入的密码哈希等）；损坏时忽略并告警。"""
    if AUTH_FILE.exists():
        try:
            return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("认证配置文件损坏，已忽略：%s", e)
    return {}

def _persist_auth(data: dict) -> None:
    """原子写入认证配置，权限固定 0600（密码文件仅 owner 可读写）。

    fnOS 的 ZFS 上，Path.write_text 可能因 ACL 产生异常 mode（0001），
    依赖 ACL 才可读、且脆。故显式 chmod 0600 + 原子 replace。
    """
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_name(AUTH_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)

_file_auth = _load_file_auth()

AUTH = {
    "hash": (os.environ.get("WV_PASSWORD_HASH", "").strip()
             or _file_auth.get("password_hash", "")),
    "secret": (os.environ.get("WV_SECRET", "").strip()
               or _file_auth.get("secret", "")
               or A.gen_secret()),
    "allow_cidrs": [c for c in
                    (os.environ.get("WV_ALLOW_CIDRS", "") or "").split(",") if c.strip()],
    "throttle": A.LoginThrottle(),
    "setup_skipped": bool(_file_auth.get("setup_skipped")),
    # 代际号：+1 就让所有已发出的票作废（怀疑泄漏 / 改密码时用）
    "epoch": int(_file_auth.get("epoch") or 1),
    # 已吊销的会话号（退出登录时登记，只针对那一张票）
    "revoked": set(_file_auth.get("revoked") or []),
}
AUTH["enabled"] = bool(AUTH["hash"])

def _persist_revoked() -> None:
    """把吊销名单写回 auth.json（与密码哈希同文件，随数据卷持久）。"""
    data = _load_file_auth()
    data["revoked"] = A.prune_revoked(AUTH["revoked"])
    data["epoch"] = AUTH["epoch"]
    try:
        _persist_auth(data)
    except OSError as e:
        log.warning("写入吊销名单失败（本次退出仅在本次进程内有效）：%s", e)

def _cookie_secure(req: Request) -> bool:
    """Cookie 是否加 secure。

    局域网普遍是 http，硬加 secure 会让 Cookie 根本存不下来（等于永远登不上）。
    所以只在**确实走 https** 时才加：显式配了 WV_COOKIE_SECURE=1，
    或请求本身是 https（含挂在反向代理后面时看 X-Forwarded-Proto）。
    """
    if os.environ.get("WV_COOKIE_SECURE", "") in ("1", "true", "True", "yes"):
        return True
    if req.url.scheme == "https":
        return True
    return (req.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
            == "https")

def _set_session_cookie(resp, req: Request, token: str) -> None:
    """下发会话 Cookie（登录/初始化/滑动续期共用，避免各处参数写歪）。"""
    resp.set_cookie(
        A.COOKIE_NAME, token, max_age=A.token_ttl(),
        httponly=True, samesite="lax", path="/",
        secure=_cookie_secure(req),
    )

# 不需要登录即可访问的路径
PUBLIC_PATHS = {
    "/login", "/login.css",
    "/api/login", "/api/auth",
    "/api/setup", "/api/setup/skip",
    "/healthz",
    "/favicon.ico", "/icon.svg",
    "/manifest.webmanifest", "/sw.js",
}

def _peer_ip(req: Request) -> str:
    """TCP 对端地址（**不可伪造**）。

    凡是"只信本机"的判断（比如 /api/reload 的回环放行）都必须用它，
    不能用 _client_ip —— 后者在开启反向代理时会去读 X-Forwarded-For，
    而那个头是客户端可以自己塞的。
    """
    return (req.client.host if req.client else "") or ""

def _client_ip(req: Request) -> str:
    """客户端 IP（本服务直接暴露在局域网，无反向代理，取连接对端即可）。

    挂在反向代理后面时，对端永远是代理自己，网段白名单和登录限流就全废了。
    那时设 WV_TRUST_PROXY=1，改从 X-Forwarded-For / X-Real-IP 取。

    ⚠️ 只有**确实**有一层可信反向代理时才开：HTTP 头是客户端能自己写的，
    直接暴露时开着它，任何人都能伪造 IP 绕过网段白名单和登录限流。
    """
    if os.environ.get("WV_TRUST_PROXY", "") not in ("1", "true", "True", "yes"):
        return _peer_ip(req)
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        # XFF 形如 "client, proxy1, proxy2"：取最左边那个（原始客户端）
        first = xff.split(",")[0].strip()
        if first:
            return first
    real = req.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return _peer_ip(req)

def _health_probe():
    """健康探测：真的去开一下库，别只会回 {"ok": true}。

    以前这里无条件 200，于是"容器在跑但库读不了"（卷没挂上、权限错、
    vault.db 被写坏）在 docker ps 里一律显示 healthy —— 监控等于没有。
    现在：库还没配置 → 200（启动早期，正常）；库配置了但打不开/查不动 → 503。
    """
    path = STATE.get("index_path")
    if not path or not os.path.exists(path):
        return JSONResponse({"ok": True, "db": "not-loaded"})
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            conn.execute("SELECT 1 FROM conv LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("健康检查打不开归档库：%s", e)
        return JSONResponse({"ok": False, "db": "unreadable",
                             "detail": str(e)[:200]}, status_code=503)
    return JSONResponse({"ok": True, "db": "ok"})

@app.middleware("http")
async def _auth_guard(req: Request, call_next):
    """统一鉴权：未登录时页面跳登录、API 回 401。"""
    path = req.url.path

    # 原因：Docker healthcheck 从容器内部发起，源 IP 是容器网段（如 172.x），
    #       既不在局域网白名单里，也不会带 Cookie。若不前置，容器会永远 unhealthy。
    # 该端点不返回任何聊天数据，暴露风险为零。
    if path == "/healthz":
        return _health_probe()

    # autosync 从容器内 127.0.0.1 调 /api/reload。它没有登录态、不带 session
    # cookie，一旦设置了访问密码就会拿到 401 —— 结果是媒体关联明明跑完了，
    # 查看器还在用旧缓存，新消息的图片/表情永远不显示。
    # 回环地址与容器同生共死，放行它不扩大攻击面；真正的入口仍由后面的
    # 网段白名单和密码守着。
    # 这里必须用**对端地址**：开了 WV_TRUST_PROXY 后 _client_ip 会读
    # X-Forwarded-For，而那个头谁都能写 —— 用它会变成"任何人都能触发重载"。
    if path == "/api/reload" and _peer_ip(req) in ("127.0.0.1", "::1"):
        return await call_next(req)

    # 尚未初始化：没设密码、也没跳过设置
    uninitialized = (not AUTH["enabled"]) and (not AUTH["setup_skipped"])

    # 网段白名单（配了才生效）—— 必须放在下面两个分支**之前**。
    # 放在"认证关闭"之后的话，"跳过密码"模式会直接 return 放行，
    # 白名单被整个绕过，配了也形同虚设。
    if AUTH["allow_cidrs"] and not A.ip_in_any(_client_ip(req), AUTH["allow_cidrs"]):
        # 未初始化时放行设置相关页面：否则管理员自己在陌生网段，
        # 连设密码的入口都进不去，服务等于废了
        if not (uninitialized and path in PUBLIC_PATHS):
            return JSONResponse({"detail": "当前网络不在允许范围内"}, status_code=403)

    # 尚未初始化 → 引导到设置页
    if uninitialized:
        if path in PUBLIC_PATHS:
            return await call_next(req)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "需要先设置访问密码", "setup": True},
                                status_code=401)
        return RedirectResponse("/login", status_code=302)

    # 认证关闭（已跳过设置）→ 直通
    if not AUTH["enabled"]:
        return await call_next(req)

    # 公开路径直通
    if path in PUBLIC_PATHS:
        return await call_next(req)

    # 校验会话
    token = req.cookies.get(A.COOKIE_NAME, "")
    if A.check_token(AUTH["secret"], token, AUTH["epoch"], AUTH["revoked"]):
        resp = await call_next(req)
        # 滑动续期：票还剩不到一天就换一张新的。
        # 主动使用者永远在线，人走了票自然过期——比"固定 30 天"更合理，
        # 也让一份泄漏出去的票最多活 7 天而不是 30 天。
        if A.token_left(AUTH["secret"], token) < A.RENEW_LEFT:
            _set_session_cookie(resp, req, A.make_token(AUTH["secret"],
                                                        epoch=AUTH["epoch"]))
        return resp

    # 未登录
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录", "auth": True}, status_code=401)
    return RedirectResponse("/login", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page():
    """登录页。"""
    f = STATIC_DIR / "login.html"
    if not f.exists():
        raise HTTPException(500, "登录页缺失")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.get("/login.css")
def login_css():
    """登录页样式。"""
    f = STATIC_DIR / "login.css"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="text/css")

@app.post("/api/login")
async def api_login(req: Request):
    """校验密码并下发会话 Cookie。"""
    if not AUTH["enabled"]:
        return {"ok": True, "auth_enabled": False}

    ip = _client_ip(req)
    locked, left = AUTH["throttle"].is_locked(ip)
    if locked:
        return JSONResponse(
            {"detail": f"尝试次数过多，请 {left} 秒后再试"}, status_code=429)

    try:
        body = await req.json()
    except Exception:
        body = {}
    pw = str(body.get("password") or "")

    if not A.verify_password(pw, AUTH["hash"]):
        AUTH["throttle"].record_fail(ip)
        # 剩余可试次数（供前端提示）
        tried = len(AUTH["throttle"]._fails.get(ip, []))
        left_tries = max(0, AUTH["throttle"].max_fails - tried)
        return JSONResponse(
            {"detail": "密码错误", "left": left_tries}, status_code=401)

    AUTH["throttle"].reset(ip)
    token = A.make_token(AUTH["secret"], epoch=AUTH["epoch"])
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, req, token)
    return resp

@app.post("/api/logout")
async def api_logout(req: Request):
    """退出登录：吊销当前这张票，并清掉 Cookie。

    原来只是 delete_cookie——那只是让**这个浏览器**忘掉 Cookie，
    票本身还是有效的：谁把它抄走（或浏览器没删干净），照样能进。
    现在把票的 sid 记进吊销名单，服务端这边也认它作废。
    传 {"all": true} 则把代际号 +1，所有设备的票一起失效
    （怀疑票泄漏、又不想改密码时用）。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    kick_all = isinstance(body, dict) and bool(body.get("all"))

    token = req.cookies.get(A.COOKIE_NAME, "")
    p = A.parse_token(AUTH["secret"], token)
    if kick_all:
        AUTH["epoch"] = int(AUTH["epoch"]) + 1
    elif p:
        AUTH["revoked"].add(p[2])
    elif token:
        # 票不是本实例签的（换过 secret、或别的实例签发的）：没有 sid 可吊销，
        # 至少把 Cookie 清掉，不留个"我退出了"的假象
        log.debug("退出登录时票无法解析，仅清理 Cookie")
    _persist_revoked()
    resp = JSONResponse({"ok": True, "kick_all": kick_all})
    resp.delete_cookie(A.COOKIE_NAME, path="/")
    return resp

def _check_origin(req: Request) -> bool:
    """校验 Origin/Referer 与请求 Host 一致（防跨站提交）。

    没有 Origin/Referer 时放行：非浏览器客户端（curl、脚本）本来就不带头，
    它们的来源由网段白名单和密码负责；浏览器则必然带头。
    """
    origin = req.headers.get("origin") or req.headers.get("referer")
    if not origin:
        return True
    host = req.headers.get("host") or ""
    if not host:
        return False
    return origin.startswith("http://" + host) or origin.startswith("https://" + host)


@app.post("/api/setup")
async def api_setup(req: Request):
    """首次初始化：设置管理员密码。仅未初始化时可用。"""
    if not _check_origin(req):
        return JSONResponse({"detail": "请求来源不允许"}, status_code=403)
    # 跳过密码也算"已初始化"——否则跳过之后任何人都能再来设一次密码，
    # 等于把接管窗口永久敞开
    if AUTH["enabled"] or AUTH["setup_skipped"]:
        return JSONResponse({"detail": "已设置过密码"}, status_code=409)
    try:
        body = await req.json()
    except Exception:
        body = {}
    pw = str(body.get("password") or "")
    if len(pw) < 8:
        return JSONResponse({"detail": "密码至少 8 位"}, status_code=400)
    new_hash = A.hash_password(pw)
    new_secret = A.gen_secret()
    try:
        _persist_auth({"password_hash": new_hash, "secret": new_secret})
    except OSError as e:
        log.warning("写入认证配置文件失败：%s", e)
        return JSONResponse({"detail": "无法写入配置文件，请检查数据目录权限"},
                            status_code=500)
    AUTH["hash"] = new_hash
    AUTH["secret"] = new_secret
    AUTH["enabled"] = True
    # secret 换了，老票的签名必然对不上；代际号归位，吊销名单也就没用了
    AUTH["epoch"] = 1
    AUTH["revoked"] = set()
    token = A.make_token(AUTH["secret"], epoch=AUTH["epoch"])
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, req, token)
    return resp

@app.post("/api/setup/skip")
async def api_setup_skip(req: Request):
    """跳过首次设置：保持无密码直进（本机/可信网络零摩擦）。"""
    if not _check_origin(req):
        return JSONResponse({"detail": "请求来源不允许"}, status_code=403)
    # 已设密码就不能再"跳过"覆盖掉；已跳过也不重复写
    if AUTH["enabled"] or AUTH["setup_skipped"]:
        return JSONResponse({"detail": "已设置过密码"}, status_code=409)
    try:
        data = {}
        if AUTH_FILE.exists():
            try:
                data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["setup_skipped"] = True
        _persist_auth(data)
    except OSError as e:
        log.warning("写入跳过标记失败：%s", e)
    AUTH["setup_skipped"] = True
    return {"ok": True}

@app.get("/api/auth")
def api_auth():
    """前端启动时探测认证状态：是否已启用、是否需要引导设置。"""
    return {"enabled": AUTH["enabled"],
            "setup": not AUTH["enabled"] and not AUTH["setup_skipped"]}

# 索引构建

def build_index(convs: list[dict], db_path: Path) -> None:
    """建立 SQLite FTS5 全文索引（trigram 分词，适合中文）。

    注意：此 schema 必须与 archiver/wv_archiver.py 的 SCHEMA **保持一致**，
    否则 --data（临时扫描）模式与 --store（生产）模式行为会不同。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE conv (
            conv_id TEXT PRIMARY KEY, account TEXT DEFAULT '', title TEXT,
            source TEXT, is_group INTEGER, members TEXT, n_msg INTEGER,
            start_ts INTEGER, end_ts INTEGER
        );
        CREATE TABLE msg (
            msg_id TEXT PRIMARY KEY, conv_id TEXT, account TEXT DEFAULT '',
            seq INTEGER, ts INTEGER, time_str TEXT, sender TEXT,
            is_self INTEGER, type TEXT, content TEXT, media TEXT
        );
        CREATE VIRTUAL TABLE msg_fts USING fts5(
            content, sender, conv_id UNINDEXED, msg_id UNINDEXED, account UNINDEXED,
            tokenize='trigram'
        );
        CREATE INDEX idx_msg_conv ON msg(conv_id, seq);
        CREATE INDEX idx_msg_ts ON msg(ts);
        CREATE INDEX idx_conv_account ON conv(account);
        CREATE INDEX idx_msg_account ON msg(account);
        """
    )
    for c in convs:
        acct = c.get("account") or DEFAULT_ACCOUNT
        conn.execute(
            "INSERT INTO conv VALUES (?,?,?,?,?,?,?,?,?)",
            (c["conv_id"], acct, c["title"], c["source"], int(c["is_group"]),
             json.dumps(c.get("members", []), ensure_ascii=False),
             len(c["messages"]), c["stats"].get("start_ts"), c["stats"].get("end_ts")),
        )
        for m in c["messages"]:
            conn.execute(
                "INSERT OR REPLACE INTO msg VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (m["msg_id"], c["conv_id"], acct, m["seq"], m["ts"], m["time_str"],
                 m["sender"], int(m["is_self"]), m["type"], m["content"], m["media"]),
            )
            if m["content"] and m["type"] in ("text", "link", "system", "other"):
                conn.execute(
                    "INSERT INTO msg_fts (content, sender, conv_id, msg_id, account) "
                    "VALUES (?,?,?,?,?)",
                    (m["content"], m["sender"], c["conv_id"], m["msg_id"], acct),
                )
    conn.commit()
    conn.close()

def load_data(data_dir: Path) -> dict:
    """加载归档目录下的全部聊天记录并建索引。

    账号从子目录推断：<data_dir>/<账号名>/xxx.txt → account = "<账号名>"，
    与 archiver/wv_archiver.py 的 _account_of() 规则保持一致。
    """
    with _lock:
        if STATE["loading"]:
            while STATE["loading"]:
                time.sleep(0.2)
            return {"conv_count": len(STATE["convs"])}
        STATE["loading"] = True
    try:
        root = data_dir.resolve()
        convs = []
        # 逐个文件解析，同时按相对路径推断账号
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".csv", ".json", ".html", ".htm", ".txt"):
                continue
            if p.name.startswith((".", "~$", "_")):
                continue
            if p.parent.resolve() == root:
                acct = DEFAULT_ACCOUNT
            else:
                rel = p.resolve().relative_to(root)
                acct = rel.parts[0].strip() or DEFAULT_ACCOUNT
            try:
                c = P.parse_file(str(p))
            except Exception as e:
                log.warning("会话文件解析失败，**该会话被整段跳过**，请检查该文件：%s", e)
                continue
            if not c.get("messages"):
                continue
            # 会话 ID 全局唯一化，避免不同账号同名会话互相覆盖
            c["account"] = acct
            c["conv_id"] = f"{acct}::{c['conv_id']}"
            convs.append(c)

        idx = data_dir / "_index.db"
        build_index(convs, idx)
        with _lock:
            STATE["convs"] = convs
            STATE["data_dir"] = str(data_dir)
            STATE["index_path"] = str(idx)
            STATE["loaded_at"] = int(time.time())
        return {
            "conv_count": len(convs),
            "msg_count": sum(len(c["messages"]) for c in convs),
            "index": str(idx),
        }
    finally:
        with _lock:
            STATE["loading"] = False

def _db() -> sqlite3.Connection:
    """打开 vault.db 连接（每请求一连接，避免跨线程复用）；未加载回 503。"""
    path = STATE.get("index_path")
    if not path or not os.path.exists(path):
        raise HTTPException(503, "数据尚未加载")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

# API

@app.get("/api/archive")
def api_archive():
    """存档轨概览：快照代次 / 文件数 / 占用空间（前端「双轨」面板用）。"""
    _, raw_snap = _raw_dirs()
    # 存档轨可被显式关闭（WV_ARCHIVE_RAW=0）
    enabled = os.environ.get("WV_ARCHIVE_RAW", "1") not in ("0", "false", "False", "")
    out = {
        "enabled": enabled,
        "root": str(raw_snap),
        "snapshots": [],
        "file_count": 0,
        "total_bytes": 0,
    }
    if not raw_snap.is_dir():
        return out

    for day_dir in sorted(raw_snap.iterdir(), reverse=True):
        if not day_dir.is_dir() or day_dir.name.startswith("."):
            continue
        n_files = 0
        n_bytes = 0
        kinds: set[str] = set()
        for p in day_dir.rglob("*"):
            if not p.is_file():
                continue
            try:
                n_bytes += p.stat().st_size
            except OSError as e:
                log.debug("统计体积时该文件不可读，已计入文件数：%s", e)
                pass
            n_files += 1
            low = p.name.lower()
            if low.endswith(".db"):
                kinds.add("Backup.db")
            elif low.startswith("bak_") and "media" in low:
                kinds.add("BAK_0_MEDIA")
            elif low.startswith("bak_") and "text" in low:
                kinds.add("BAK_0_TEXT")
            elif low.startswith("bak_"):
                kinds.add("BAK_*")
            elif low.endswith(".bak"):
                kinds.add(".bak")
        out["snapshots"].append({
            "day": day_dir.name,
            "n_files": n_files,
            "bytes": n_bytes,
            "kind": " / ".join(sorted(kinds)) or "备份包",
        })
        out["file_count"] += n_files
        out["total_bytes"] += n_bytes
    return out

@app.get("/api/status")
def api_status():
    """当前数据规模与加载状态（前端顶栏与启动自检用）。"""
    mode = STATE.get("mode", "scan")
    if mode == "store":
        msg_count = STATE.get("msg_count", 0)
    else:
        msg_count = sum(len(c.get("messages", [])) for c in STATE["convs"])
    return {
        "data_dir": STATE["data_dir"],
        "mode": mode,
        "conv_count": len(STATE["convs"]),
        "msg_count": msg_count,
        "account_count": len({c.get("account") or DEFAULT_ACCOUNT
                              for c in STATE["convs"]}),
        "loaded_at": STATE["loaded_at"],
        "loading": STATE["loading"],
    }

# 微信运行时管理（单容器一体化：安装/查看微信，接手机迁移）

WECHAT_CTL = "/woc/wechat-runtime.sh"
WECHAT_STATUS = Path("/data/wechat/.state/status.json")

@app.get("/api/wechat/status")
def wechat_status():
    """查询微信运行时状态（是否已安装、版本、安装进度）"""
    if WECHAT_STATUS.exists():
        try:
            return json.loads(WECHAT_STATUS.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug("微信状态文件损坏，回退实时探测：%s", e)
            pass
    installed = Path("/data/wechat/opt/wechat/wechat").exists()
    return {"phase": "idle", "percent": 0, "installed": installed,
            "version": "", "message": "尚未安装" if not installed else "已安装"}

@app.post("/api/wechat/install")
def wechat_install(background: BackgroundTasks):
    """触发微信下载安装（后台执行，前端轮询 status 看进度）"""
    if not Path(WECHAT_CTL).exists():
        raise HTTPException(500, "微信运行时脚本缺失（镜像构建异常）")
    if STATE.get("wechat_installing"):
        return {"ok": True, "msg": "安装已在进行中"}

    def _run():
        """后台任务：装完/超时后清掉 installing 标志，让按钮可再次点击。"""
        STATE["wechat_installing"] = True
        try:
            subprocess.run(["/bin/bash", WECHAT_CTL, "install"],
                           timeout=3600, capture_output=True)
        finally:
            STATE["wechat_installing"] = False

    STATE["wechat_installing"] = True
    background.add_task(_run)
    return {"ok": True, "msg": "已开始下载安装微信（约 190~210MB）"}

@app.get("/wechat-desktop")
def wechat_desktop(req: Request):
    """跳转到本机 KasmVNC 微信桌面（自动沿用当前访问的主机名，只换端口）"""
    host = (req.url.hostname or "127.0.0.1")
    # 宿主侧端口由 compose 注入（WV_KASMVNC_PORT，默认 13000；
    # 宿主 3000 被 NapCat 占用，故默认不再用 3000）
    kasm = os.environ.get("WV_KASMVNC_PORT", "13000")
    return RedirectResponse(f"http://{host}:{kasm}/")

def _ensure_schema(db: Path) -> None:
    """旧库自愈：补齐 account 等新增列，避免 `no such column: account`。

    查看器是只读的，但**允许**做这种无损 schema 升级 —— 否则一个旧库
    就能让服务起不来。真正的数据写入仍归归档器。
    """
    def cols(c, table: str) -> set[str]:
        """某表现有列名（表不存在返回空集）。"""
        try:
            return {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    with sqlite3.connect(str(db)) as c:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        changed = False

        for table, col, decl in (
            ("source_file", "account", "TEXT DEFAULT ''"),
            ("source_file", "conv_id", "TEXT"),
            ("source_file", "n_added", "INTEGER DEFAULT 0"),
            ("conv", "account", "TEXT DEFAULT ''"),
            ("msg", "account", "TEXT DEFAULT ''"),
            ("msg", "media_full", "TEXT"),
            # 显示名会重名（实测 199 组），头像必须靠 username 才能精确对上
            ("msg", "sender_username", "TEXT DEFAULT ''"),
            # 会话的稳定身份：同步链路用它做 conv_id 的键
            ("conv", "username", "TEXT DEFAULT ''"),
        ):
            if table in tables and col not in cols(c, table):
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                changed = True

        # 排序索引：--data（临时扫描）模式建的库以前只有 idx_msg_conv_ts，
        # 缺 (conv_id, ts, seq, msg_id) 的完整排序索引 —— 于是 /api/messages
        # 只能取出整个会话再排序取前 200（18 万条的群聊就是全取一次）。
        # store 模式由 wv_sync.ensure_schema 建，这里补 scan 模式的那一半。
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(conv_id, ts)",
            "CREATE INDEX IF NOT EXISTS idx_msg_conv_ts_full"
            " ON msg(conv_id, ts, seq, msg_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_ts ON msg(ts)",
            "CREATE INDEX IF NOT EXISTS idx_conv_account ON conv(account)",
        ):
            try:
                c.execute(ddl)
            except sqlite3.Error as e:
                log.debug("补索引跳过（%s）：%s", ddl.split("IF NOT EXISTS")[1].strip(), e)

        fts = cols(c, "msg_fts")
        if fts and "account" not in fts:
            # FTS5 不支持 ADD COLUMN → 整表重建（内容从 msg 全量回填，不丢）
            c.executescript("""
                DROP TABLE IF EXISTS msg_fts;
                CREATE VIRTUAL TABLE msg_fts USING fts5(
                    content, sender, conv_id UNINDEXED, msg_id UNINDEXED,
                    account UNINDEXED, tokenize='trigram'
                );
                INSERT INTO msg_fts (content, sender, conv_id, msg_id, account)
                    SELECT content, sender, conv_id, msg_id,
                           COALESCE(account, '') FROM msg;
            """)
            changed = True

        # 补齐索引（自愈没有索引的库）。idx_msg_conv_ts_full 覆盖
        # /api/messages 的完整排序键，缺 seq/msg_id 时会退化成 TEMP B-TREE
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(conv_id, ts)",
            "CREATE INDEX IF NOT EXISTS idx_msg_conv_ts_full"
            " ON msg(conv_id, ts, seq, msg_id)",
            "CREATE INDEX IF NOT EXISTS idx_msg_ts ON msg(ts)",
        ):
            try:
                c.execute(idx_sql)
            except sqlite3.Error:
                pass        # 表不存在等：下次启动再试，不阻断启动

        if "account" in cols(c, "conv"):
            c.execute("UPDATE conv SET account=? WHERE account IS NULL OR account=''",
                      (DEFAULT_ACCOUNT,))
        if "account" in cols(c, "msg"):
            c.execute("UPDATE msg SET account=? WHERE account IS NULL OR account=''",
                      (DEFAULT_ACCOUNT,))
        if changed:
            c.commit()

def load_store(store_dir: Path) -> dict:
    """
    直接使用归档库（vault.db）—— 服务启动不再重建索引，秒级就绪。

    这是生产模式：归档器负责入库，查看器只读。
    """
    db = store_dir / "vault.db"
    if not db.exists():
        raise FileNotFoundError(f"归档库不存在: {db}（请先运行 wv_archiver scan）")

    _ensure_schema(db)

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        convs = [dict(r) for r in conn.execute(
            "SELECT conv_id, account, title, is_group, members, n_msg, start_ts, end_ts "
            "FROM conv ORDER BY end_ts DESC")]
        total = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]

    for c in convs:
        try:
            c["members"] = json.loads(c.get("members") or "[]")
        except Exception:
            c["members"] = []
        c["is_group"] = bool(c["is_group"])
        c["source"] = "vault"   # 前端展示用（归档库不区分来源格式）
        c["account"] = c.get("account") or DEFAULT_ACCOUNT
        c["title"] = c["title"] or c["conv_id"]

    with _lock:
        STATE["convs"] = convs          # 仅用于侧栏列表与标题查找
        STATE["data_dir"] = str(store_dir)
        STATE["index_path"] = str(db)   # 查询直接走 vault.db
        STATE["loaded_at"] = int(time.time())
        STATE["mode"] = "store"
        STATE["msg_count"] = total

    return {"conv_count": len(convs), "msg_count": total, "mode": "store"}

@app.post("/api/reload")
def api_reload():
    """重新加载数据（归档器更新库后可调用，无需重启服务）。"""
    if not STATE["data_dir"]:
        raise HTTPException(400, "未配置数据目录")
    # 归档进行中时不重载：那会儿 vault.db 正在被写，读到的是写了一半的库。
    # 给它 20 秒耐心（重载本身很快，撞上归档才需要等）；等不到就报错，
    # autosync 下一轮还会再来一次，不会永久丢。
    if not _HEAVY_LOCK.acquire(timeout=_HEAVY_LOCK_TIMEOUT):
        raise HTTPException(409, "归档任务正在运行，请稍后重试")
    try:
        d = Path(STATE["data_dir"])
        if STATE.get("mode") == "store":
            res = load_store(d)
        else:
            res = load_data(d)
        _invalidate_derived_caches()
        return {"ok": True, **res}
    finally:
        _HEAVY_LOCK.release()

# 投放口路径（供 /api/ingest 使用）：默认为归档库同级的 inbox，
# 可用环境变量 WV_INBOX 覆盖。存档轨投放口为 inbox/raw。
def _inbox_dir() -> Path:
    """可读轨投放目录。"""
    env = os.environ.get("WV_INBOX")
    if env:
        return Path(env)
    return Path(STATE["data_dir"]) / "inbox"

def _raw_dirs() -> tuple[Path, Path]:
    """返回（存档轨投放口, 存档轨归档目录）。"""
    raw_in = os.environ.get("WV_RAW_INBOX") or str(_inbox_dir() / "raw")
    raw_snap = os.environ.get("WV_RAW_SNAPSHOTS") or str(Path(STATE["data_dir"]) / "raw-snapshots")
    return Path(raw_in), Path(raw_snap)

@app.post("/api/ingest")
def api_ingest():
    """立即归档收件箱里的新文件（真正的实现在 _do_ingest，本函数只管并发）。

    归档全程独占：详见文件上方 _HEAVY_LOCK 的注释。
    """
    if not _HEAVY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有归档/重载任务在运行，请稍后再试")
    try:
        return _do_ingest()
    finally:
        _HEAVY_LOCK.release()

def _do_ingest():
    """立即把收件箱里的新文件归档入库（不等定时任务）。

    这是 Windows 端「一键投递」调用的接口 —— 投递完文件后触发，
    归档 → 重载，网页刷新即可看到。

    同时处理两条轨：
      ① 可读轨：inbox/<账号>/*.txt|html|csv|json  → vault.db
      ② 存档轨：inbox/raw/**  → raw-snapshots/YYYY-MM-DD/
    """
    import subprocess
    if not STATE["data_dir"]:
        raise HTTPException(400, "未配置数据目录")
    store = Path(STATE["data_dir"])
    inbox = _inbox_dir()
    if not inbox.is_dir():
        raise HTTPException(400, f"收件箱不存在: {inbox}")

    here = Path(__file__).resolve().parent.parent / "archiver"
    archiver = here / "wv_archiver.py"
    raw_arch = here / "wv_raw.py"
    if not archiver.is_file():
        raise HTTPException(500, f"归档器缺失: {archiver}")

    results: dict = {}

    # ① 可读轨：注意要跳过 inbox/raw（存档轨的内容不该被当聊天记录解析）
    try:
        p1 = subprocess.run(
            [sys.executable, str(archiver), "scan",
             "--source", str(inbox), "--store", str(store),
             "--exclude-dir", "raw"],
            capture_output=True, text=True, timeout=300,
        )
        results["readable"] = {
            "ok": p1.returncode == 0,
            "returncode": p1.returncode,
            "stdout_tail": (p1.stdout or "").strip().splitlines()[-6:],
            "stderr_tail": (p1.stderr or "").strip().splitlines()[-4:],
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "可读轨归档超时（>300s）")

    # ② 存档轨（可选；wv_raw.py 不存在时跳过）
    raw_in, raw_snap = _raw_dirs()
    if raw_arch.is_file() and raw_in.is_dir():
        try:
            p2 = subprocess.run(
                [sys.executable, str(raw_arch), "ingest",
                 "--source", str(raw_in), "--dest", str(raw_snap),
                 "--manifest", str(store / "MANIFEST.json")],
                capture_output=True, text=True, timeout=900,
            )
            results["raw"] = {
                "ok": p2.returncode == 0,
                "returncode": p2.returncode,
                "stdout_tail": (p2.stdout or "").strip().splitlines()[-5:],
                "stderr_tail": (p2.stderr or "").strip().splitlines()[-4:],
            }
        except subprocess.TimeoutExpired:
            results["raw"] = {"ok": False, "error": "存档轨归档超时（>900s）"}

    # ③ 媒体链路：新图解密 / 消息关联 / WxAM 原图 / 视频·语音·文件
    media_py = here / "wv_media.py"
    if media_py.is_file():
        py_media = os.environ.get("WV_MEDIA_PYTHON", "/opt/wx-venv/bin/python3")
        plain_dbs = Path(os.environ.get("WV_PLAIN_DBS", "/data/wx_backup/plain_dbs"))
        wx_files = Path(os.environ.get("WV_WECHAT_FILES", "/config/xwechat_files"))
        media_out = Path(os.environ.get("WV_MEDIA", str(store / "media")))
        if not os.path.exists(py_media):
            results["media"] = {"ok": False, "error": "媒体解释器缺失: %s" % py_media}
        elif not plain_dbs.is_dir():
            results["media"] = {"ok": False,
                                "error": "解密明文库不存在: %s" % plain_dbs}
        else:
            accs = []
            for acct in sorted(wx_files.glob("*/")):
                attach = acct / "msg" / "attach"
                if not attach.is_dir():
                    continue
                seg = {"account": acct.name, "ok": True, "steps": []}
                for cmd in ("all", "extras"):
                    try:
                        p = subprocess.run(
                            [py_media, str(media_py), cmd,
                             "--attach", str(attach),
                             "--account-root", str(acct),
                             "--vault", str(store / "vault.db"),
                             "--plain-dbs", str(plain_dbs),
                             "--out", str(media_out)],
                            capture_output=True, text=True, timeout=1800)
                        seg["steps"].append({
                            "cmd": cmd, "returncode": p.returncode,
                            "tail": (p.stdout or "").strip().splitlines()[-4:],
                        })
                        if p.returncode != 0:
                            seg["ok"] = False
                    except subprocess.TimeoutExpired:
                        seg["steps"].append({"cmd": cmd, "error": "超时（>1800s）"})
                        seg["ok"] = False
                accs.append(seg)
            results["media"] = {
                "ok": all(a["ok"] for a in accs) if accs else True,
                "accounts": accs,
            }

    # 归档完立即重载，让页面拿到新数据
    res = load_store(store) if STATE.get("mode") == "store" else load_data(store)
    _invalidate_derived_caches()

    return {
        "ok": results.get("readable", {}).get("ok", False),
        **results,
        **res,
    }

@app.get("/api/accounts")
def api_accounts():
    """账号列表（多账号隔离：前端据此渲染分栏切换器）。"""
    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT account, COUNT(*) n_conv, COALESCE(SUM(n_msg),0) n_msg, "
            "MIN(start_ts) first_ts, MAX(end_ts) last_ts "
            "FROM conv GROUP BY account ORDER BY n_msg DESC")]
    finally:
        conn.close()
    total = sum(r["n_msg"] for r in rows)
    return {"items": rows, "total": total,
            "all": {"account": "", "alias": "全部账号", "n_conv": len(STATE["convs"]),
                    "n_msg": total}}

@app.get("/api/conversations")
def api_conversations(
    q: str = Query("", description="按标题过滤"),
    account: str = Query("", description="限定账号（为空则全部）"),
    sort: str = Query("recent", description="recent | count | name"),
    limit: int = Query(500, ge=1, le=5000),
):
    """会话列表（左侧栏），支持按账号过滤。"""
    sql = "SELECT * FROM conv"
    where: list[str] = []
    params: list = []
    if q:
        where.append("title LIKE ?")
        params.append(f"%{q}%")
    if account:
        where.append("account=?")
        params.append(account)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + {
        "recent": "end_ts DESC",
        "count": "n_msg DESC",
        "name": "title ASC",
    }.get(sort, "end_ts DESC")
    sql += " LIMIT ?"
    params.append(limit)

    conn = _db()
    try:
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
    for r in rows:
        try:
            r["members"] = json.loads(r.get("members") or "[]")
        except Exception:
            r["members"] = []
        r["is_group"] = bool(r["is_group"])
        r.setdefault("source", "vault")
        r.setdefault("account", "")
    return {"items": rows, "total": len(rows)}

@app.get("/api/messages")
def api_messages(
    conv_id: str = Query(..., description="会话 ID"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=2000),
    order: str = Query("asc", description="asc | desc"),
):
    """分页拉取某会话的消息。

    排序必须按 ts，不能按 seq：seq 来自两套互不相干的编号——JSON 导入的是
    文件内序号、微信库同步的是 local_id，两者混在同一会话里时大小关系没有意义。
    按 seq 排会把 10 月初导入的消息排到 10 月底同步的消息前面，
    表现为「前端最新一条停在很早的日期」。seq 只作为同秒消息的稳定 tiebreaker。
    """
    direction = "DESC" if order == "desc" else "ASC"
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM msg WHERE conv_id=?", (conv_id,)
        ).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM msg WHERE conv_id=?"
            f" ORDER BY ts {direction}, seq {direction}, msg_id {direction}"
            f" LIMIT ? OFFSET ?",
            (conv_id, limit, offset),
        )]
    if order == "desc":
        rows.reverse()  # 保证返回时始终按时间正序，便于前端拼接
    for r in rows:
        r["is_self"] = bool(r["is_self"])
    _sanitize_rows(rows)
    return {"conv_id": conv_id, "total": total, "offset": offset,
            "limit": limit, "has_more": offset + len(rows) < total, "items": rows}

@app.get("/api/search")
def api_search(
    q: str = Query("", description="关键词；留空则退化为按类型/日期筛选"),
    conv_id: str = Query("", description="限定会话（可选）"),
    account: str = Query("", description="限定账号（可选）"),
    types: str = Query("", description="消息类型，逗号分隔，如 image,video"),
    date_from: str = Query("", description="起始日期 YYYY-MM-DD（含）"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD（含）"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """「查找聊天记录」数据源：关键词 + 类型 + 日期区间 组合筛选。

    中文走 trigram FTS；**关键词短于 3 个字符时必须回落 LIKE**，原因见
    `FTS_MIN_CHARS`；FTS 不可用时同样回落。关键词为空时退化成纯条件浏览
    —— 这正是「按日期查找聊天记录」的用法。
    """
    q = (q or "").strip()
    type_list = [t.strip() for t in (types or "").split(",") if t.strip()]
    lo, hi = _ts_bounds(date_from, date_to)

    def _conds(alias: str) -> tuple[str, list]:
        """按给定表别名拼 WHERE 片段（msg 与 msg_fts 两处复用同一套过滤）。"""
        sql, params = "", []
        if conv_id:
            sql += f" AND {alias}conv_id=?"
            params.append(conv_id)
        if account:
            sql += f" AND {alias}account=?"
            params.append(account)
        if type_list:
            sql += f" AND {alias}type IN ({','.join('?' * len(type_list))})"
            params.extend(type_list)
        if lo:
            sql += f" AND {alias}ts>=?"
            params.append(lo)
        if hi:
            sql += f" AND {alias}ts<?"
            params.append(hi)
        return sql, params

    hits: list[dict] = []
    total = 0
    with _db() as conn:
        # 「FTS 能用」和「FTS 有命中」必须分开看。
        # 原写法 fts_ok = total > 0：关键词搜不到时 total=0 → 判成 FTS 不可用
        # → 掉进 LIKE 分支，对 189 万行做一次全表 content LIKE '%x%'（几秒起步）。
        # 实际上 FTS 明明跑通了，答案就是「没有」。只有 FTS 真的不可用
        # （表不存在 / MATCH 语法错）或压根没关键词时，才需要 LIKE 兜底。
        #
        # 另一个坑（实测踩到）：trigram 分词器把文本切成**三个字符**一组的
        # token，所以短于 3 个字符的查询**永远命中 0 条**——它不报错，只返回空。
        # 中文最常用的词恰好是两个字（快递、会议、老师、吃饭），于是
        # `len(q) >= 2` 这个判断让这些查询"成功地"查出 0 条，并且因为
        # fts_usable=True 永远不回落 LIKE，搜索等于完全失效。
        # 所以长度门槛必须是 FTS_MIN_CHARS（3），不是 2。
        fts_usable = False
        if q and len(q) >= FTS_MIN_CHARS:
            where, fparams = _conds("m.")
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM msg_fts f JOIN msg m ON m.msg_id=f.msg_id "
                    "WHERE msg_fts MATCH ?" + where, [f'"{q}"'] + fparams).fetchone()[0]
                fts_usable = True
            except sqlite3.OperationalError:
                total, fts_usable = 0, False
        if fts_usable:
            where, fparams = _conds("m.")
            hits = [dict(r) for r in conn.execute(
                "SELECT m.* FROM msg_fts f JOIN msg m ON m.msg_id=f.msg_id "
                "WHERE msg_fts MATCH ?" + where +
                " ORDER BY m.ts DESC, m.seq DESC LIMIT ? OFFSET ?",
                [f'"{q}"'] + fparams + [limit, offset])]
        else:
            where, lparams = _conds("")
            if q:
                where = " AND content LIKE ?" + where
                lparams = [f"%{q}%"] + lparams
            total = conn.execute(
                "SELECT COUNT(*) FROM msg WHERE 1=1" + where, lparams).fetchone()[0]
            hits = [dict(r) for r in conn.execute(
                "SELECT * FROM msg WHERE 1=1" + where +
                " ORDER BY ts DESC, seq DESC LIMIT ? OFFSET ?",
                lparams + [limit, offset])]
    for h in hits:
        h["is_self"] = bool(h["is_self"])
        h.setdefault("account", "")
    _sanitize_rows(hits)
    return {"query": q, "count": len(hits), "total": total, "offset": offset,
            "limit": limit, "has_more": offset + len(hits) < total, "items": hits}

@app.get("/api/calendar")
def api_calendar(
    conv_id: str = Query("", description="限定会话（可选）"),
    account: str = Query("", description="限定账号（可选）"),
    limit: int = Query(4000, ge=1, le=20000),
):
    """按自然日（东八区）聚合的消息量日历 —— 「按日期查找聊天记录」用。

    返回逐日条数与分类计数，供前端渲染**月历**（与原生微信一致的日期速查）。
    默认上限 4000 天（≈11 年），足够覆盖全量历史；全库跨度 757 天。

    结果按 (conv_id, account, limit) 缓存：日期面板会被反复开关，而聚合结果
    在同一批数据下是稳定的，没必要每次重扫一遍整个会话。归档/重载时失效。
    """
    key = (conv_id, account, limit)
    cached = _CAL_CACHE.get(key)
    if cached is not None:
        return cached
    conds, params = ["ts IS NOT NULL"], []
    if conv_id:
        conds.append("conv_id=?")
        params.append(conv_id)
    if account:
        conds.append("account=?")
        params.append(account)
    where = " WHERE " + " AND ".join(conds)
    with _db() as conn:
        days = [dict(r) for r in conn.execute(
            f"SELECT {DAY_EXPR} d, COUNT(*) n, "
            "SUM(type='image') img, SUM(type='video') vid, "
            "SUM(type='file') fil, SUM(type='link') lnk, SUM(type='voice') voi "
            f"FROM msg{where} GROUP BY d ORDER BY d DESC LIMIT ?", params + [limit])]
        rng = conn.execute(
            f"SELECT MIN(ts) a, MAX(ts) b FROM msg{where}", params).fetchone()
    fmt = lambda t: dt.datetime.fromtimestamp(t, TZ8).strftime("%Y-%m-%d") if t else ""
    result = {"days": days, "first": fmt(rng["a"]), "last": fmt(rng["b"])}
    _CAL_CACHE[key] = result
    return result

@app.get("/api/locate")
def api_locate(
    conv_id: str = Query(..., description="会话 ID"),
    date: str = Query("", description="目标日期 YYYY-MM-DD"),
    ts: int = Query(0, description="目标时间戳（优先于 date，供搜索结果直接跳转）"),
):
    """定位某会话中「指定日期 / 指定时间戳」的首条消息（含其升序下标）。

    前端拿到 index 后即可用既有 offset 分页直接取到该处的消息窗口。
    """
    if ts:
        lo, day = int(ts), dt.datetime.fromtimestamp(int(ts), TZ8).strftime("%Y-%m-%d")
    else:
        lo, _ = _day_bounds(date)
        day = date
        if not lo:
            raise HTTPException(400, "需提供 date(YYYY-MM-DD) 或 ts")
    with _db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM msg WHERE conv_id=?", (conv_id,)).fetchone()[0]
        day_count = conn.execute(
            "SELECT COUNT(*) FROM msg WHERE conv_id=? AND ts>=? AND ts<?",
            (conv_id, lo, lo + 86400)).fetchone()[0]
        row = conn.execute(
            "SELECT seq, ts, msg_id FROM msg WHERE conv_id=? AND ts>=?"
            # 排序口径必须和 /api/messages 完全一致（ts, seq, msg_id）。
            # 少一个 msg_id 时，同秒多条的场景下这里选中的"当天第一条"
            # 和前端列表里的第一条可能不是同一条，回填下标就错位。
            " ORDER BY ts, seq, msg_id LIMIT 1",
            (conv_id, lo)).fetchone()
        if not row:
            return {"date": day, "found": False, "total": total,
                    "day_count": day_count, "index": 0}
        index = conn.execute(
            "SELECT COUNT(*) FROM msg WHERE conv_id=? AND seq<?",
            (conv_id, row["seq"])).fetchone()[0]
    return {"date": day, "found": True, "total": total, "day_count": day_count,
            "index": index, "seq": row["seq"], "ts": row["ts"], "msg_id": row["msg_id"]}

_STATS_CACHE: dict[tuple, dict] = {}
# 日历聚合缓存：/api/calendar 要按天扫一整个会话（18 万条的群聊就是 18 万行）。
# 日期面板会被反复开关，结果在同一批数据下是稳定的，没必要每次重扫。
_CAL_CACHE: dict[tuple, dict] = {}

def _invalidate_derived_caches() -> None:
    """归档 / 重载后清空派生缓存（统计/日历结果只在数据变化时失效）。"""
    _STATS_CACHE.clear()
    _CAL_CACHE.clear()

@app.get("/api/stats")
def api_stats(
    conv_id: str = Query("", description="为空则全库统计"),
    account: str = Query("", description="限定账号（可选）"),
):
    """统计报告，支持按账号过滤。

    性能：原来用 6 条独立 SQL 各扫一遍 133 万行（冷态 7.93s）。
    现在 ① 把 总数/时间范围/收发比 合成一次扫描，
        ② 结果按 (conv_id, account) 缓存，归档后失效。
    """
    key = (conv_id, account)
    cached = _STATS_CACHE.get(key)
    if cached is not None:
        return cached

    with _db() as conn:
        where, params = "", []
        conds = []
        if conv_id:
            conds.append("conv_id=?")
            params.append(conv_id)
        if account:
            conds.append("account=?")
            params.append(account)
        if conds:
            where = " WHERE " + " AND ".join(conds)
        # 一次扫描同时拿到 总数 / 最早 / 最晚 / 自己发的条数
        agg = conn.execute(
            f"SELECT COUNT(*) c, MIN(ts) a, MAX(ts) b, SUM(is_self) s FROM msg{where}",
            params).fetchone()
        total = agg["c"]
        if not total:
            return {"total": 0}
        row = agg
        by_type = [dict(r) for r in conn.execute(
            f"SELECT type, COUNT(*) n FROM msg{where} GROUP BY type ORDER BY n DESC",
            params)]
        by_sender = [dict(r) for r in conn.execute(
            f"SELECT sender, COUNT(*) n FROM msg{where} GROUP BY sender "
            f"ORDER BY n DESC LIMIT 30", params)]
        # 按账号分布（仅在未限定账号时给出）
        by_account = []
        if not account:
            by_account = [dict(r) for r in conn.execute(
                "SELECT account, COUNT(*) n FROM msg GROUP BY account ORDER BY n DESC")]
        # 按小时分布
        hour_conds = ["ts IS NOT NULL"]
        hour_params: list = []
        if conv_id:
            hour_conds.append("conv_id=?")
            hour_params.append(conv_id)
        if account:
            hour_conds.append("account=?")
            hour_params.append(account)
        hour_sql = (
            "SELECT CAST(strftime('%H', ts, 'unixepoch', '" + TZ_SQL + "') AS INTEGER) h, "
            "COUNT(*) n FROM msg WHERE " + " AND ".join(hour_conds) +
            " GROUP BY h ORDER BY h"
        )
        by_hour = [dict(r) for r in conn.execute(hour_sql, hour_params)]
    result = {
        "total": total,
        "self": row["s"] or 0,
        "other": total - (row["s"] or 0),
        "start": P.fmt_time(row["a"]),
        "end": P.fmt_time(row["b"]),
        "by_type": by_type,
        "by_sender": by_sender,
        "by_account": by_account,
        "by_hour": by_hour,
    }
    _STATS_CACHE[key] = result
    return result

# 导出（HTML / CSV / TXT）

def _iter_convs(conv_id: str = "", account: str = ""):
    """逐会话产出「会话头 + 该会话全部消息」（生成器）。

    为什么改生成器：旧实现 `_fetch_convs` 把所有会话的全部消息一次性读进
    list —— 133 万条消息光 Python dict 开销就有 400–650 MB。
    逐会话 yield 后，内存峰值只与「最大单个会话」相关。
    连接的生命周期由生成器持有，迭代结束/提前关闭时 finally 关闭。
    """
    conn = _db()
    try:
        where, params = [], []
        if conv_id:
            where.append("conv_id=?")
            params.append(conv_id)
        if account:
            where.append("account=?")
            params.append(account)
        sql = ("SELECT conv_id, account, title, n_msg, start_ts, end_ts "
               "FROM conv")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY end_ts DESC"
        heads = [dict(r) for r in conn.execute(sql, params)]

        for h in heads:
            h["messages"] = [dict(r) for r in conn.execute(
                "SELECT msg_id, seq, ts, time_str, sender, is_self, type, "
                "content, media FROM msg WHERE conv_id=? "
                "ORDER BY COALESCE(ts,0), seq", (h["conv_id"],))]
            # 导出会生成可离线打开的 HTML 文件，里面的 <a href> 是可点的，
            # 必须和 Web API 出口一样过 scheme 白名单
            _sanitize_rows(h["messages"])
            h["account"] = h.get("account") or DEFAULT_ACCOUNT
            yield h
    finally:
        conn.close()

def _fetch_convs(conv_id: str = "", account: str = "") -> list[dict]:
    """按条件取出会话 + 全部消息（保留给需要整体列表的调用方）。"""
    return list(_iter_convs(conv_id=conv_id, account=account))

def _safe_name(s: str, limit: int = 60) -> str:
    """把会话名变成安全文件名（去路径分隔符与控制字符）。"""
    bad = '<>:"/\\|?*\x00-\x1f'
    s = "".join("_" if c in bad else c for c in str(s or "session"))
    s = s.strip().strip(".") or "session"
    return s[:limit]

def _dl(content: bytes | str, filename: str, mime: str,
        inline: bool = False) -> Response:
    """下载响应（RFC 5987 编码文件名，中文不乱码）。"""
    from urllib.parse import quote
    if isinstance(content, str):
        content = content.encode("utf-8")
    disp = "inline" if inline else "attachment"
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Content-Disposition":
                f"{disp}; filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(len(content)),
        },
    )

@app.get("/api/export/conv")
def export_conv(
    conv_id: str = Query(..., description="会话 ID"),
    fmt: str = Query("html", description="html | csv | txt"),
    inline_media: bool = Query(False, description="HTML 是否内嵌图片 base64"),
):
    """导出单个会话。"""
    convs = _fetch_convs(conv_id=conv_id)
    if not convs:
        raise HTTPException(404, "会话不存在")
    c = convs[0]
    name = _safe_name(f"{c.get('account') or ''}_{c.get('title') or c['conv_id']}")
    fmt = (fmt or "html").lower()

    if fmt == "csv":
        return _dl(X.conv_to_csv(c), f"{name}.csv",
                   "text/csv; charset=utf-8")
    if fmt == "txt":
        return _dl(X.conv_to_txt(c), f"{name}.txt",
                   "text/plain; charset=utf-8")
    mroot = _media_root()
    return _dl(X.conv_to_html(c, inline_media=inline_media, media_root=mroot),
               f"{name}.html", "text/html; charset=utf-8", inline=True)

@app.get("/api/export/all")
def export_all(
    account: str = Query("", description="限定账号（为空则全部）"),
    fmt: str = Query("html", description="html | csv | txt"),
    inline_media: bool = Query(False),
):
    """导出全部（或某账号全部）会话（逐会话流式，避免全量消息驻留内存）。"""
    it = _iter_convs(account=account)
    first = next(it, None)
    if first is None:
        raise HTTPException(404, "没有可导出的会话")

    def _all():
        """把首元素与生成器重新拼成一个流（先取 first 是为了判空 404）。"""
        yield first
        yield from it

    convs = _all()
    tag = account or "全部账号"
    name = _safe_name(f"微信聊天记录_{tag}_{time.strftime('%Y%m%d')}")
    fmt = (fmt or "html").lower()

    if fmt == "csv":
        return _dl(X.convs_to_csv(convs), f"{name}.csv",
                   "text/csv; charset=utf-8")
    if fmt == "txt":
        def _gen_txt():
            """逐会话 yield 文本，会话之间加分隔线。"""
            first = True
            for c in convs:
                if not first:
                    yield "\n" + "=" * 60 + "\n\n"
                first = False
                yield X.conv_to_txt(c).decode("utf-8")
        return _dl_stream(_gen_txt(), f"{name}.txt",
                          "text/plain; charset=utf-8")

    # HTML：多会话合成单文件（顺序拼接，便于整体打印为 PDF）
    # 流式输出：逐个会话 yield，内存里只留当前这一段
    mroot = _media_root()

    def _gen_html():
        """逐会话 yield HTML 片段，拼成单文件（可整体打印为 PDF）。"""
        yield _HTML_HEAD + f"<title>微信聊天记录 · {tag}</title>" \
                           f"<style>{X._CSS}</style></head><body>"
        first = True
        for c in convs:
            if not first:
                yield _HTML_SEP
            first = False
            html = X.conv_to_html(c, inline_media=inline_media, media_root=mroot)
            i = html.find('<div class="doc">')
            j = html.rfind("</body>")
            yield html[i:j] if (i >= 0 and j > i) else html
        yield f"<script>{X._JS}</script></body></html>"

    return _dl_stream(_gen_html(), f"{name}.html",
                      "text/html; charset=utf-8", inline=True)

@app.get("/api/export/manifest")
def export_manifest():
    """导出会话清单（JSON），便于批量处理或做备份目录索引。

    只读 conv 表（几百行），不要走 _fetch_convs ——
    那会把 133 万条消息全部读进内存，只为拿会话标题。
    """
    conn = _db()
    try:
        convs = [dict(r) for r in conn.execute(
            "SELECT conv_id, account, title, n_msg, start_ts, end_ts "
            "FROM conv ORDER BY end_ts DESC")]
    finally:
        conn.close()
    items = [{k: c.get(k) for k in
              ("conv_id", "account", "title", "n_msg", "start_ts", "end_ts")}
             for c in convs]
    payload = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_convs": len(items),
        "total_msgs": sum(i["n_msg"] or 0 for i in items),
        "accounts": sorted({i["account"] for i in items}),
        "items": items,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return _dl(body, f"会话清单_{time.strftime('%Y%m%d')}.json",
               "application/json; charset=utf-8")

def _dl_stream(gen, filename: str, mime: str,
               inline: bool = False) -> Response:
    """流式下载响应：逐块吐出，不把整个文件拼进内存。

    全库导出时正文约 25 MB，加 HTML 标签后 100 MB+；
    旧实现 `"".join(parts)` 会在内存里同时持有各段 + 合并后的整串，
    峰值可达 200–400 MB。流式输出后峰值只剩当前会话那一小段。
    """
    from urllib.parse import quote
    from fastapi.responses import StreamingResponse
    disp = "inline" if inline else "attachment"
    return StreamingResponse(
        gen,
        media_type=mime,
        headers={"Content-Disposition":
                 f"{disp}; filename*=UTF-8''{quote(filename)}"},
    )

_HTML_HEAD = (
    "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
)
_HTML_SEP = ('<div style="height:26px"></div>'
             '<div style="max-width:760px;margin:0 auto;border-top:2px dashed #ddd">'
             "</div>")

def _merge_html(parts: list[str], title: str) -> str:
    """把多个单会话 HTML 合成一个（去掉重复的 head/body，拼 mid-body）。"""
    bodies = []
    for p in parts:
        i = p.find('<div class="doc">')
        j = p.rfind("</body>")
        seg = p[i:j] if i >= 0 and j > i else p
        bodies.append(seg)
    sep = ('<div style="height:26px"></div>'
           '<div style="max-width:760px;margin:0 auto;border-top:2px dashed #ddd">'
           "</div>")
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{title}</title><style>{X._CSS}</style></head><body>"
        + sep.join(bodies)
        + f"<script>{X._JS}</script></body></html>"
    )

def _media_root() -> Path | None:
    """媒体根目录：优先 WV_MEDIA，其次归档库同级 media/。"""
    store = STATE.get("data_dir")
    env = os.environ.get("WV_MEDIA")
    root = Path(env) if env else (Path(store) / "media" if store else None)
    return root

# 前端页面

@app.get("/", response_class=HTMLResponse)
def index():
    """主界面外壳。"""
    f = STATIC_DIR / "index.html"
    if not f.exists():
        raise HTTPException(500, "前端文件缺失")
    # no-cache：每次协商验证。缺了它浏览器启发式缓存旧外壳，
    # 局域网 IP 是不安全上下文（SW 不可用），前端更新将永远推不到客户端
    return HTMLResponse(f.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})

@app.get("/app.js")
def appjs():
    """主界面脚本。"""
    f = STATIC_DIR / "app.js"
    if not f.exists():
        raise HTTPException(500, "前端文件缺失")
    return FileResponse(f, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

@app.get("/app.css")
def appcss():
    """主界面样式。"""
    f = STATIC_DIR / "app.css"
    if not f.exists():
        raise HTTPException(500, "前端文件缺失")
    return FileResponse(f, media_type="text/css",
                        headers={"Cache-Control": "no-cache"})

# ── PWA ──

@app.get("/sw.js")
def service_worker():
    """Service Worker（缓存策略在 sw.js 内定义）。"""
    f = STATIC_DIR / "sw.js"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(
        f, media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/manifest.webmanifest")
def manifest():
    """PWA 清单。"""
    f = STATIC_DIR / "manifest.webmanifest"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="application/manifest+json")

@app.get("/icon.svg")
def icon():
    """应用图标。"""
    f = STATIC_DIR / "icon.svg"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="image/svg+xml")

@app.get("/favicon.ico")
def favicon():
    """站点图标（直接复用 SVG 图标，现代浏览器都认）。"""
    # 复用 SVG 图标（现代浏览器都认）
    f = STATIC_DIR / "icon.svg"
    if not f.exists():
        raise HTTPException(404, "not found")
    return FileResponse(f, media_type="image/svg+xml")

# 媒体类型映射（解码后的图片/语音/视频）
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".amr": "audio/amr", ".silk": "audio/silk", ".bin": "application/octet-stream",
}

_AVATAR_INDEX: dict = {}

def _media_dir() -> Path | None:
    """媒体根目录（与归档器 WV_MEDIA 保持一致）。"""
    env = os.environ.get("WV_MEDIA")
    if env:
        return Path(env).resolve()
    store = STATE.get("data_dir")
    return (Path(store) / "media").resolve() if store else None

def _avatar_index() -> dict:
    """显示名/wxid → 头像文件名（由 wv_media.py avatars 生成）。"""
    global _AVATAR_INDEX
    if _AVATAR_INDEX:
        return _AVATAR_INDEX
    root = _media_dir()
    p = (root / "avatar_index.json") if root else None
    if p and p.is_file():
        try:
            _AVATAR_INDEX = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _AVATAR_INDEX = {}
    return _AVATAR_INDEX

@app.get("/api/avatar")
def api_avatar(name: str = Query("", description="联系人显示名或 wxid")):
    """联系人头像（取自微信 head_image 缓存）。

    数据来源：`wv_media.py avatars` 把 head_image.db 的 image_buffer
    落成文件并生成 avatar_index.json。没有头像返回 404，前端自动回退色块。
    """
    root = _media_dir()
    if not root:
        raise HTTPException(404, "媒体目录未配置")
    fn = _avatar_index().get(name)
    if not fn:
        raise HTTPException(404, "无头像")
    target = (root / "avatars" / fn)
    if not target.is_file():
        raise HTTPException(404, "无头像")
    mime = _MEDIA_TYPES.get(target.suffix.lower(), "image/jpeg")
    return FileResponse(target, media_type=mime)

@app.get("/media/{rel:path}")
def media(rel: str):
    """提供解码后的媒体文件（图片/语音/视频）。

    媒体固定位于 <store>/media 之下（设计文档规定的结构）。
    安全：严格限制在该目录内，阻止路径穿越。
    """
    store = STATE.get("data_dir")
    if not store:
        raise HTTPException(503, "数据尚未加载")
    # 允许通过环境变量覆盖（与归档器 WV_MEDIA 保持一致）
    root = _media_dir()
    if not root:
        raise HTTPException(503, "媒体目录未配置")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)          # 越界即抛 ValueError
    except ValueError:
        raise HTTPException(403, "非法路径")
    if not target.is_file():
        raise HTTPException(404, "媒体不存在")
    mime = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=mime)

# 入口

def main():
    """CLI 入口：--store 直读 vault.db（生产），--data 临时解析目录。"""
    ap = argparse.ArgumentParser(description="WeChat Vault 查看器")
    ap.add_argument("--data", help="聊天记录目录（临时模式：启动时解析并建索引）")
    ap.add_argument("--store", help="归档库目录（生产模式：直接读 vault.db，秒级启动）")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认仅本机；局域网访问用 0.0.0.0（注意安全）")
    args = ap.parse_args()

    if not args.data and not args.store:
        ap.error("必须指定 --store（推荐）或 --data")

    if args.store:
        store_dir = Path(args.store).expanduser().resolve()
        print(f"归档库模式: {store_dir / 'vault.db'}")
        try:
            res = load_store(store_dir)
        except FileNotFoundError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        print(f"完成：{res['conv_count']} 个会话 / {res['msg_count']} 条消息")
    else:
        data_dir = Path(args.data).expanduser().resolve()
        if not data_dir.is_dir():
            print(f"数据目录不存在: {data_dir}", file=sys.stderr)
            sys.exit(1)
        STATE["mode"] = "scan"
        STATE["data_dir"] = str(data_dir)
        print(f"正在解析归档目录: {data_dir} ...")
        t0 = time.time()
        res = load_data(data_dir)
        print(f"完成：{res['conv_count']} 个会话 / {res['msg_count']} 条消息 "
              f"（{time.time()-t0:.1f}s）")

    print(f"\n  WeChat Vault 已启动 →  http://{args.host}:{args.port}")
    if AUTH["enabled"]:
        print(f"  访问控制：已启用（网段白名单 "
              f"{AUTH['allow_cidrs'] or '不限'}）")
    else:
        print("  访问控制：未启用（未设 WV_PASSWORD_HASH）")
    print()
    # 默认 warning 会让 500 的错误栈完全不可见（排错时极易误判），
    # 故默认提到 info；需要安静可用 WV_LOG_LEVEL=warning
    level = os.environ.get("WV_LOG_LEVEL", "info")
    uvicorn.run(app, host=args.host, port=args.port, log_level=level)

if __name__ == "__main__":
    main()
