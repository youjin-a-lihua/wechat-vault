#!/usr/bin/env bash
# =============================================================================
# 微信数据方舟 · 提交前体检（preflight）
# -----------------------------------------------------------------------------
# 目的：把「代码审查标准」里**机器可判定**的那部分自动化，
#       让审查者只在人类才能判断的问题上花时间。
#
# 用法：
#     bash tools/preflight.sh            # 全量检查
#     bash tools/preflight.sh --staged   # 只查即将提交的文件（配 pre-commit）
#
# 退出码：0 = 通过；1 = 存在 🔴 阻断项（不应提交/合并）
# =============================================================================
set -uo pipefail

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; RST=$'\033[0m'
BLOCK=0
WARN=0

say()  { printf '%s\n' "$*"; }
blk()  { printf '%s🔴 %s%s\n' "$RED" "$*" "$RST"; BLOCK=$((BLOCK+1)); }
wrn()  { printf '%s🟡 %s%s\n' "$YEL" "$*" "$RST"; WARN=$((WARN+1)); }
ok()   { printf '%s✅ %s%s\n' "$GRN" "$*" "$RST"; }
hdr()  { printf '\n%s── %s %s\n' "$DIM" "$*" "$RST"; }

# 待检查的 Python 文件（排除第三方 vendor 与缓存）
pyfiles() {
  if [[ "${1:-}" == "--staged" ]]; then
    git diff --cached --name-only --diff-filter=ACM 2>/dev/null | grep '\.py$' || true
  else
    find . -name '*.py' -not -path '*/vendor/*' -not -path '*/__pycache__/*' \
           -not -path './.git/*' 2>/dev/null
  fi
}

PY=${PY:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python

# =============================================================================
hdr "① 凭据扫描（阻断级）"
# -----------------------------------------------------------------------------
# 教训（2026-10-02）：仓库里真实存在 KasmVNC 口令、sudo 口令、NAS 内网 IP、
# 账号级解密密钥与个人 wxid，且当时**没有 .gitignore**。这类泄漏一旦 push 无法撤回。
#
# 设计要点（低误报是硬要求——噪声大的检查器会被无视）：
#   · 本脚本**不内置任何真实凭据**（否则扫描器自己就是一份密钥清单）
#   · 私有网段匹配会**排除 CIDR**（`192.168.0.0/16` 这类网段声明不算泄漏）
#   · 凭据只认**引号字面量**或长裸 token —— `os.environ.get("X")`、函数调用一律不算
#   · 占位符（**** … change your_ $ { <）自动跳过
#   · 确需保留的行写 `preflight-allow` 显式豁免（可审计、可 grep）
#   · 示例地址统一用 RFC 5737 文档保留段（192.0.2.x / 198.51.100.x / 203.0.113.x）
# -----------------------------------------------------------------------------
LEAK=$("$PY" - <<'PYEOF'
import os, re, fnmatch

SKIP_DIRS = {'vendor', '__pycache__', '.git', 'node_modules', '.venv'}
SKIP_FILES = {'deploy/.env', '.env', 'env.example', 'tools/preflight.sh',
              '.preflight-denylist'}
SCAN_EXT = {'.py', '.js', '.html', '.css', '.yml', '.yaml', '.json', '.sh',
            '.ps1', '.cmd', '.bat', '.md', '.txt', '.toml', '.ini', '.cfg', '.example'}

# ── 私有网段：排除 CIDR（IP 后面跟 /nn）──────────────────
RX_IP = re.compile(
    r'(?<![\d.])(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)'
    r'\.\d{1,3}\.\d{1,3}(?![\d.])(?!\s*/)')

RX_PKEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')

# ── 凭据：只认引号字面量 / 长裸 token，排除函数调用与环境变量读取 ──
RX_CRED = re.compile(
    r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\b\s*[=:]\s*'
    r'(?:"([^"]{6,})"|\'([^\']{6,})\'|([A-Za-z0-9!@#$%^&*_+\-]{12,}))')

PLACEHOLDER = re.compile(
    r'(?i)(\*{3,}|\.\.\.|change|your_|your-|xxx|example|placeholder|^<|^\$|^\{)')

ALLOW = 'preflight-allow'          # 行内显式豁免标记

def is_placeholder(v):
    return bool(PLACEHOLDER.search(v))

def gitignored(rel):
    gi = '.gitignore'
    if not os.path.exists(gi):
        return False
    base = os.path.basename(rel)
    for line in open(gi, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        pat = line.rstrip('/')
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat) \
           or rel.endswith('/' + pat):
            return True
    return False

deny = []
if os.path.exists('.preflight-denylist'):
    for line in open('.preflight-denylist', encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#'):
            deny.append(re.compile(line))

hits = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        p = os.path.join(root, f)
        rel = p[2:] if p.startswith('./') else p
        rel = rel.replace(os.sep, '/')
        if rel in SKIP_FILES:
            continue
        # 原则：只扫「会被提交的文件」——.gitignore 里的内容本就不该上传，
        # 对它报警只会制造噪声（deploy/.env 的真值正是要放那里的）。
        if gitignored(rel):
            continue
        # 原则：只扫「会被提交的文件」——.gitignore 里的内容本就不该上传，
        # 对它报警只会制造噪声（deploy/.env 的真值正是要放那里的）。
        if gitignored(rel):
            continue
        ext = os.path.splitext(f)[1].lower()
        if ext and ext not in SCAN_EXT:
            continue
        try:
            lines = open(p, encoding='utf-8', errors='replace').read().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if ALLOW in line:
                continue
            tag = None
            if RX_IP.search(line):
                tag = '私有网段'
            elif RX_PKEY.search(line):
                tag = '私钥'
            else:
                m = RX_CRED.search(line)
                if m:
                    val = next(g for g in m.groups()[1:] if g)
                    if not is_placeholder(val):
                        tag = '凭据赋值'
            if tag is None and any(rx.search(line) for rx in deny):
                tag = '黑名单'
            if tag:
                hits.append('%s:%d  [%s] %s' % (rel, i, tag, line.strip()[:110]))
print('\n'.join(hits))
PYEOF
)

if [[ -n "$LEAK" ]]; then
  N=$(printf '%s\n' "$LEAK" | wc -l)
  blk "发现 $N 处凭据/内网地址："
  printf '%s\n' "$LEAK" | head -20 | sed 's/^/     /'
  [[ "$N" -gt 20 ]] && say "     …（仅显示前 20 条）"
  say "     → 真值移到 deploy/.env（已被忽略）；示例地址改用 192.0.2.x 等文档保留段"
  say "     → 确需保留的行写 preflight-allow；项目特有标识写 .preflight-denylist"
else
  ok "无凭据、无内网地址、无内联私钥"
fi

# 凭据类文件（直接比对 .gitignore，不依赖 git 仓库已初始化）
KEYFILES=$(find . \( -name '*pwhash*' -o -name '*.pem' -o -name '*.key' -o -name 'id_rsa*' \
           -o -name '.env' -o -name 'credentials*' \) -not -path './.git/*' 2>/dev/null || true)
if [[ -n "$KEYFILES" ]]; then
  UNIGNORED=""
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    rel="${f#./}"
    if ! grep -qE "(^|/)$(basename "$rel")\$|^$(basename "$rel")\$" .gitignore 2>/dev/null \
       && ! grep -qF "$(basename "$rel")" .gitignore 2>/dev/null; then
      UNIGNORED="$UNIGNORED$f"$'\n'
    fi
  done <<< "$KEYFILES"
  if [[ -n "$UNIGNORED" ]]; then
    blk "存在未被 .gitignore 覆盖的凭据类文件："
    printf '%s' "$UNIGNORED" | sed 's/^/     /'
    say "     → 删除并加入 .gitignore；若已提交过，必须**轮换该凭据**（改文件不等于撤回）"
  else
    ok "凭据类文件均已被 .gitignore 忽略（如 deploy/.env）"
  fi
else
  ok "无凭据类文件"
fi

hdr "② 必须被忽略的目录是否真的忽略了"
for d in local_keys store samples data media; do
  [[ -d "$d" ]] || continue
  if [[ -f .gitignore ]] && grep -qE "^${d}/" .gitignore; then
    ok "$d/ 已在 .gitignore"
  else
    blk "$d/ 存在但未被忽略（体积/隐私风险）"
  fi
done

# =============================================================================
hdr "③ 语法与导入期错误（阻断级）"
# ⚠️ 注意：**不要**用 `-W error`。它会把 SyntaxWarning 升级成致命错误，
#    于是告警被误报成「语法错误」（本脚本初版就是这个 bug，首跑即误判）。
#    正确做法：正常编译，然后按输出内容区分 SyntaxError / SyntaxWarning。
BAD=0; WARNED=0
while IFS= read -r f; do
  [[ -n "$f" ]] || continue
  OUT=$("$PY" -m py_compile "$f" 2>&1); RC=$?
  if grep -q 'SyntaxError' <<<"$OUT"; then
    blk "语法错误（不可解析）$f"
    printf '%s\n' "$OUT" | sed 's/^/     /' | head -4
    BAD=$((BAD+1))
  elif [[ $RC -ne 0 ]]; then
    blk "编译失败 $f"
    printf '%s\n' "$OUT" | sed 's/^/     /' | head -4
    BAD=$((BAD+1))
  elif grep -q 'SyntaxWarning' <<<"$OUT"; then
    WARNED=$((WARNED+1))
    wrn "语法告警 $f（可运行，但建议修）"
    printf '%s\n' "$OUT" | grep 'SyntaxWarning' | sed 's/^/     /' | head -2
  fi
done < <(pyfiles)
[[ $BAD -eq 0 ]] && ok "全部文件可解析（无语法错误）"
[[ $WARNED -gt 0 ]] && say "     → 常见成因：普通字符串里写了正则转义（如 \"\\d\"），改成 r\"...\" 即可"
# 清理 py_compile 产物
find . -name '__pycache__' -not -path './.git/*' -type d -exec rm -rf {} + 2>/dev/null || true

# =============================================================================
hdr "④ 静默失败（阻断级）"
# -----------------------------------------------------------------------------
# 教训：本项目 98 处 except 里有 51 处宽泛捕获、29 处直接吞掉。
# 2026-10-02 排查「关联 0 条」时，正是靠 print 才发现是空结果而非异常；
# 若当时是 `except: pass`，这个 bug 会一直隐身。
# -----------------------------------------------------------------------------
NAL=$(grep -rnE '^\s*except[^:]*:\s*$' -A1 --include='*.py' archiver parser viewer tools 2>/dev/null \
      | grep -B1 -E '^\S+[-:][0-9]+[-:]\s*(pass|continue)\s*$' || true)
if [[ -n "$NAL" ]]; then
  blk "有 except 直接吞掉异常且不留痕："
  printf '%s\n' "$NAL" | sed 's/^/     /' | head -12
  say "     → 至少 log.warning(..., exc_info=True)，或注明为何可安全忽略"
else
  ok "无静默吞异常"
fi

NARROW=$(grep -rn 'except Exception\|except BaseException' --include='*.py' archiver parser viewer tools 2>/dev/null | wc -l | tr -d ' ')
if [[ "$NARROW" -gt 20 ]]; then
  wrn "宽泛捕获 except Exception 共 $NARROW 处（>20）；建议收敛到具体异常类型"
else
  ok "宽泛捕获 $NARROW 处，量级可接受"
fi

# =============================================================================
hdr "⑤ 结构与可维护性"
BIG=$("$PY" - <<'PYEOF'
import ast, os, sys
rows = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('vendor', '__pycache__', '.git', 'node_modules')]
    for f in files:
        if not f.endswith('.py'):
            continue
        p = os.path.join(root, f)
        try:
            t = ast.parse(open(p, encoding='utf-8').read())
        except Exception:
            continue
        for n in ast.walk(t):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ln = (n.end_lineno or n.lineno) - n.lineno + 1
                if ln > 80:
                    rows.append((ln, p, n.name))
rows.sort(reverse=True)
for ln, p, n in rows[:10]:
    print('%4d 行  %s :: %s' % (ln, p, n))
PYEOF
)
if [[ -n "$BIG" ]]; then
  wrn "超过 80 行的函数（越长的函数越难审、越难测）："
  printf '%s\n' "$BIG" | sed 's/^/     /'
else
  ok "无超长函数"
fi

PRINTS=$(grep -rc 'print(' --include='*.py' archiver parser viewer 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
LOGS=$(grep -rn 'logging\.' --include='*.py' archiver parser viewer 2>/dev/null | wc -l | tr -d ' ')
if [[ "$LOGS" -eq 0 && "$PRINTS" -gt 50 ]]; then
  wrn "日志全用 print（$PRINTS 处）且没有 logging —— 无法分级、无法过滤、无法上报告警"
else
  ok "日志：print=$PRINTS logging=$LOGS"
fi

# =============================================================================
hdr "⑥ 依赖与可复现性（阻断级）"
if [[ -f requirements.txt || -f pyproject.toml ]]; then
  ok "存在依赖声明文件"
  [[ -f requirements.txt ]] && ! grep -q '==' requirements.txt \
    && wrn "requirements.txt 未锁定版本（用 == 固定），构建不可复现"
else
  blk "没有 requirements.txt / pyproject.toml —— 第三方依赖（pycryptodome、zstandard…）无法复现安装"
fi

# =============================================================================
hdr "⑦ SQL 与命令拼接"
SQLI=$(grep -rnE 'execute\(f"|execute\(f'"'"'|execute\(".*%s.*" *%' --include='*.py' archiver parser viewer 2>/dev/null || true)
if [[ -n "$SQLI" ]]; then
  wrn "存在 f-string 拼进 SQL（当前来源是内部常量尚安全，但这是**注入的温床**）："
  printf '%s\n' "$SQLI" | sed 's/^/     /' | head -8
  say "     → 表名/列名走白名单常量，值一律用占位符参数"
else
  ok "未见 SQL 字符串拼接"
fi

SH=$(grep -rn 'shell=True\|os.system' --include='*.py' archiver parser viewer tools 2>/dev/null || true)
if [[ -n "$SH" ]]; then
  blk "存在 shell=True / os.system（命令注入面）："
  printf '%s\n' "$SH" | sed 's/^/     /'
else
  ok "无 shell=True / os.system（子进程均以参数列表调用）"
fi

# =============================================================================
hdr "⑧ 前端"
if [[ -f viewer/static/app.js ]]; then
  INLINE=$(grep -oE 'on(click|error|load|change)="' viewer/static/app.js 2>/dev/null | sort -u | tr '\n' ' ')
  if [[ -n "$INLINE" ]]; then
    wrn "前端存在内联事件处理器（$INLINE）—— 与 CSP 冲突、不利调试"
    say "     → 改用 addEventListener；仅 1~2 处可先记账"
  else
    ok "前端无内联事件处理器"
  fi
  command -v node >/dev/null 2>&1 && { node --check viewer/static/app.js && ok "app.js 语法通过"; }
fi

# =============================================================================
say ""
say "════════════════════════════════════════════════════"
if [[ $BLOCK -gt 0 ]]; then
  printf '%s❌ 不通过：%d 个阻断项，%d 个建议项%s\n' "$RED" "$BLOCK" "$WARN" "$RST"
  say "   阻断项必须先修，才能进入人工审查。"
  exit 1
else
  printf '%s✅ 通过：0 个阻断项，%d 个建议项%s\n' "$GRN" "$WARN" "$RST"
  say "   可以进入人工审查（建议项可留作 PR 评论）。"
  exit 0
fi
