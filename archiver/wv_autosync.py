#!/usr/bin/env python3
"""监视微信数据库变化并自动同步到方舟。

常驻进程（s6 longrun `svc-autosync`，随容器启动、崩溃自拉起）：
每隔 POLL_SEC 对微信消息库取一次 (文件名, 大小, mtime) 指纹，发现变化后
「等 STABLE_SEC 确认微信写完 → wv_sync 解密入库 → wv_media 关联媒体
→ POST /api/reload 让查看器热加载」。

设计约束：
  - 单实例：锁文件 /tmp/wv_autosync.lock，崩溃残留自动接管
  - 幂等：指纹持久化在 autosync.state，重启后不重复同步旧数据
  - 失败退避：一轮失败后 RETRY_SEC 内不再尝试，避免日志风暴
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("wv_autosync")

HERE = Path(__file__).resolve().parent


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """读环境变量为 int；非法值回退默认，结果不低于 minimum。"""
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


POLL_SEC = _env_int("WV_AUTOSYNC_POLL", 60, 5)
STABLE_SEC = _env_int("WV_AUTOSYNC_STABLE", 30, 0)
RETRY_SEC = _env_int("WV_AUTOSYNC_RETRY", 300, 30)


def _reload_url() -> str:
    """查看器重载地址：优先取显式配置，其次复用服务自身的绑定地址。"""
    explicit = os.environ.get("WV_AUTOSYNC_RELOAD")
    if explicit:
        return explicit
    port = os.environ.get("WV_PORT", "8790")
    bind = os.environ.get("WV_BIND_IP", "")
    # 0.0.0.0 表示监听全部网卡，此时回环可用
    host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
    return "http://%s:%s/api/reload" % (host, port)


RELOAD_URL = _reload_url()

WECHAT_FILES = Path(os.environ.get("WV_WECHAT_FILES", "/config/xwechat_files"))
STORE = Path(os.environ.get("WV_STORE", "/data"))
VAULT = STORE / "vault.db"
PLAIN_DBS = Path(os.environ.get("WV_PLAIN_DBS", str(STORE / "wx_backup" / "plain_dbs")))
KEYS = Path(os.environ.get("WV_KEYS_JSON", str(STORE / "wx_backup" / "all_keys.json")))
MEDIA_OUT = Path(os.environ.get("WV_MEDIA", str(STORE / "media")))
STATE_FILE = Path(os.environ.get("WV_AUTOSYNC_STATE", str(STORE / "logs" / "autosync.state")))
LOCK_FILE = Path(os.environ.get("WV_AUTOSYNC_LOCK", "/tmp/wv_autosync.lock"))
PYTHON = os.environ.get("WV_SYNC_PYTHON", "/opt/wx-venv/bin/python3")
MEDIA_PYTHON = os.environ.get("WV_MEDIA_PYTHON", PYTHON)


def db_files() -> list[Path]:
    """微信账号目录下所有消息库。"""
    out: list[Path] = []
    if not WECHAT_FILES.is_dir():
        return out
    for account in sorted(WECHAT_FILES.iterdir()):
        msg_dir = account / "db_storage" / "message"
        if not msg_dir.is_dir():
            continue
        out.extend(sorted(msg_dir.glob("message_*.db")))
    return out


def fingerprint(files: list[Path]) -> str:
    """文件的 (大小, mtime) 摘要，变化即代表有新数据。"""
    parts = []
    for f in files:
        try:
            st = f.stat()
        except OSError:
            log.debug("库文件不可读，跳过：%s", f)
            continue
        parts.append("%s:%d:%d" % (f.name, st.st_size, int(st.st_mtime)))
    return "|".join(parts)


def read_state() -> dict:
    """读上次同步的指纹；文件不存在或损坏都视为从未同步过。"""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(data: dict) -> None:
    """原子写状态：先写 .tmp 再 replace，防止留下半截 JSON。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def acquire_lock() -> bool:
    """进程级单实例锁：防止容器重启或手动触发时并发写库。"""
    try:
        if LOCK_FILE.exists():
            pid = int(LOCK_FILE.read_text().strip() or 0)
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    return False
                except OSError:
                    log.debug("锁文件残留（进程 %s 已不存在），接管", pid)
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError as e:
        log.warning("无法创建锁文件 %s（%s），继续运行", LOCK_FILE, e)
        return True


def release_lock() -> None:
    """删除自己持有的锁；锁属于别的进程时不动它。"""
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError as e:
        log.debug("释放锁失败：%s", e)


def _run(cmd: list[str], timeout: int) -> tuple[bool, str]:
    """跑子进程并取 stdout/stderr 尾部各三行拼成一行摘要。"""
    log.info("执行：%s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "超时（>%ds）" % timeout
    except OSError as e:
        return False, "无法执行：%s" % e
    # 子进程的进度日志走 stderr，结果汇总走 stdout，两者都要看
    out_tail = (p.stdout or "").strip().splitlines()[-3:]
    err_tail = (p.stderr or "").strip().splitlines()[-3:]
    if p.returncode != 0:
        detail = err_tail or out_tail
        return False, "返回码 %d，%s" % (p.returncode, " / ".join(detail))
    return True, " / ".join(out_tail or err_tail)


def sync_messages() -> tuple[bool, str]:
    """解密 + 消息入库，实际执行 wv_sync.py story。"""
    return _run([PYTHON, str(HERE / "wv_sync.py"), "story",
                 "--wechat-files", str(WECHAT_FILES),
                 "--keys", str(KEYS),
                 "--vault", str(VAULT),
                 "--plain-out", str(PLAIN_DBS)], timeout=3600)


def sync_media() -> tuple[bool, str]:
    """逐账号跑媒体关联（wv_media.py all）；单账号失败不中断其余。"""
    media = HERE / "wv_media.py"
    if not media.is_file():
        return True, "无媒体脚本，跳过"
    if not PLAIN_DBS.is_dir():
        return False, "明文库不存在：%s" % PLAIN_DBS
    results = []
    ok_all = True
    for account in sorted(WECHAT_FILES.iterdir()) if WECHAT_FILES.is_dir() else []:
        attach = account / "msg" / "attach"
        if not attach.is_dir():
            continue
        ok, info = _run([MEDIA_PYTHON, str(media), "all",
                         "--attach", str(attach),
                         "--account-root", str(account),
                         "--vault", str(VAULT),
                         "--plain-dbs", str(PLAIN_DBS),
                         "--out", str(MEDIA_OUT)], timeout=3600)
        results.append("%s=%s" % (account.name, "ok" if ok else "失败"))
        ok_all = ok_all and ok
        if not ok:
            log.warning("媒体同步失败（%s）：%s", account.name, info)
    return ok_all, "，".join(results) or "无附件目录"


def reload_viewer() -> tuple[bool, str]:
    """通知查看器重载，失败不影响同步结果。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(RELOAD_URL, method="POST")
        with opener.open(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
        return True, "会话 %s / 消息 %s" % (body.get("conv_count", "?"), body.get("msg_count", "?"))
    except Exception as e:
        return False, "重载失败：%s" % str(e)[:100]


def one_round() -> bool:
    """完整一轮同步：消息失败即整轮失败；媒体/重载失败仅告警。

    返回 True 才会推进指纹，失败时由主循环安排 RETRY_SEC 后重试。
    """
    files = db_files()
    if not files:
        log.warning("未发现微信消息库：%s", WECHAT_FILES)
        return False
    log.info("开始同步（%d 个库）", len(files))
    t0 = time.time()

    ok, info = sync_messages()
    if not ok:
        log.error("消息同步失败：%s", info)
        return False

    ok_media, info_media = sync_media()
    if not ok_media:
        log.warning("媒体同步未完全成功：%s", info_media)

    ok_reload, info_reload = reload_viewer()
    if ok_reload:
        log.info("查看器已重载：%s", info_reload)
    else:
        log.warning("%s", info_reload)

    log.info("同步完成（%.1fs）：%s", time.time() - t0, info)
    return True


def main() -> int:
    """日志双写（stdout + 文件）→ 开关检查 → 单实例锁 → 轮询主循环。"""
    level = os.environ.get("WV_AUTOSYNC_LOGLEVEL", "INFO").upper()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = os.environ.get("WV_AUTOSYNC_LOG",
                              str(Path(os.environ.get("WV_LOGS", "/data/logs")) / "autosync.log"))
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        # 轮转：autosync 是长跑进程，一轮一轮地写，不封顶的话这个日志文件
        # 会无限长（落在数据卷上，最终把卷撑满 → 微信库和归档库一起写不进去）
        import logging.handlers as _lh
        mb = float(os.environ.get("WV_LOG_MAX_MB", "20"))
        keep = int(os.environ.get("WV_LOG_KEEP", "3"))
        handlers.append(_lh.RotatingFileHandler(
            log_file, maxBytes=int(mb * 1024 * 1024),
            backupCount=max(1, keep), encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("日志文件不可写（%s），仅输出到标准输出：%s" % (log_file, e),
              file=sys.stderr)
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers)

    if os.environ.get("WV_AUTOSYNC", "1") in ("0", "false", "False", "no"):
        log.info("自动同步已关闭（WV_AUTOSYNC=0），进程退出")
        return 0

    if not acquire_lock():
        log.error("已有同步进程在运行，退出")
        return 1

    log.info("自动同步已启动：轮询 %ds，稳定判定 %ds，库目录 %s",
             POLL_SEC, STABLE_SEC, WECHAT_FILES)

    state = read_state()
    last_done = state.get("fingerprint", "")
    next_retry = 0

    try:
        while True:
            time.sleep(POLL_SEC)
            files = db_files()
            if not files:
                continue

            cur = fingerprint(files)
            if cur == last_done:
                continue
            if time.time() < next_retry:
                continue

            if STABLE_SEC:
                log.info("检测到变化，等待 %ds 确认写入完成", STABLE_SEC)
                time.sleep(STABLE_SEC)
                cur = fingerprint(db_files())
                if cur == last_done:
                    log.info("数据回退到上次状态，忽略本次变化")
                    continue

            if one_round():
                last_done = cur
                write_state({"fingerprint": last_done, "synced_at": int(time.time())})
                next_retry = 0
            else:
                next_retry = time.time() + RETRY_SEC
                log.warning("%ds 后重试", RETRY_SEC)
    except KeyboardInterrupt:
        log.info("收到中断信号，退出")
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
