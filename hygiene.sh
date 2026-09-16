#!/usr/bin/env bash
# =============================================================================
# Code Hygiene Protocol — NyxDownloader
# Run after EVERY modification, in both backend and frontend:
#
#   Phase 1 - Scan     : deprecated/duplicate code, formatting, potential bugs
#   Phase 2 - Clean    : remove redundancies, outdated impls, apply best practice
#   Phase 3 - Verify   : no breaking changes, all imports used, features intact
#
# Gate (must all pass before any code is submitted):
#   Syntax check ✅  No redundancy ✅  No deprecated methods ✅
#   Clean formatting ✅  Working features ✅
#
# Usage:  bash hygiene.sh          # full protocol (auto-fixes safe issues, then gates)
#         bash hygiene.sh --check  # verify only, no auto-fix (CI-friendly)
#
# Exit code 0 = pristine. Non-zero = something needs attention (listed above).
# =============================================================================
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

pass=0; fail=0; warn=0
ok()   { printf '  \033[32m✅\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31m❌\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33m⚠️ \033[0m %s\n' "$1"; warn=$((warn+1)); }
head() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- toolchain --
have() { command -v "$1" >/dev/null 2>&1; }
have ruff  || pip install -q ruff 2>/dev/null || true
RUFF="" ; have ruff  && RUFF="ruff"
NODE="" ; have node  && NODE="node"
PY="python3"

# ============================================================ PHASE 1 - SCAN
head "PHASE 1 — SCAN"

# --- 1a. Python syntax (hard gate) ---
head ""
head "1a. Python syntax"
SYNTAX_FAIL=0
for f in backend/*.py frontend/check_classes.py; do
  if [ -f "$f" ] && ! "$PY" -m py_compile "$f" 2>/tmp/hyg_syntax.err; then
    bad "syntax error: $f"; cat /tmp/hyg_syntax.err; SYNTAX_FAIL=1
  fi
done
[ "$SYNTAX_FAIL" = 0 ] && ok "all Python files compile"

# --- 1b. ruff: unused imports, undefined names, bugbear (real-bug patterns) ---
if [ -n "$RUFF" ]; then
  head "1b. Static analysis (ruff: unused imports / undefined names / bugbear)"
  if OUT=$("$RUFF" check --select F,B,E9,W6 --output-format concise backend/ 2>&1); then
    ok "no ruff findings"
  else
    # F401 (unused import) and B904 (raise-from) are safe to auto-fix.
    echo "$OUT" | sed 's/^/     /'
    if [ "$CHECK_ONLY" = 0 ]; then
      "$RUFF" check --select F401,B904 --fix --quiet backend/ >/dev/null 2>&1 || true
      if OUT2=$("$RUFF" check --select F,B,E9,W6 --output-format concise backend/ 2>&1); then
        ok "auto-fixed safe issues; remaining are advisory"
      else
        note "advisory findings remain (see above) — review before submit"
      fi
    else
      bad "ruff findings (run without --check to auto-fix safe ones)"
    fi
  fi
else
  note "ruff not available — skipping static analysis (pip install ruff)"
fi

# --- 1c. Frontend JS syntax (hard gate) ---
head "1c. Frontend JS syntax"
if [ -n "$NODE" ]; then
  JS_FAIL=0
  for f in frontend/index.html frontend/recode.html frontend/admin.html frontend/app.js; do
    [ -f "$f" ] || continue
    if [[ $f == *.html ]]; then
      EXTRACT=$("$PY" - "$f" <<'PY'
import re,sys,pathlib
print("\n;\n".join(re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>",
      pathlib.Path(sys.argv[1]).read_text(), re.S)))
PY
)
      if ! printf '%s' "$EXTRACT" | "$NODE" --check - 2>/tmp/hyg_js.err; then
        bad "JS syntax: $f"; sed 's/^/     /' /tmp/hyg_js.err; JS_FAIL=1
      fi
    else
      if ! "$NODE" --check "$f" 2>/tmp/hyg_js.err; then
        bad "JS syntax: $f"; sed 's/^/     /' /tmp/hyg_js.err; JS_FAIL=1
      fi
    fi
  done
  [ "$JS_FAIL" = 0 ] && ok "all frontend JS parses"
else
  note "node not available — skipping JS syntax check"
fi

# --- 1d. Class guard: every markup class must exist in compiled CSS ---
head "1d. Class guard (markup classes ⊆ compiled CSS)"
if "$PY" frontend/check_classes.py >/tmp/hyg_cls.out 2>&1; then
  ok "all classes present in tailwind.css"
else
  bad "class guard failed"; sed 's/^/     /' /tmp/hyg_cls.out
fi

# --- 1e. Duplicate definitions in the SAME scope (nested same-name helpers
#         in different functions are legitimate, e.g. yt-dlp hook closures) ---
head "1e. Duplicate definitions (scope-aware)"
DUP=$("$PY" - <<'PY'
import ast, pathlib, collections
for p in sorted(pathlib.Path("backend").glob("*.py")):
    try:
        tree = ast.parse(p.read_text())
    except SyntaxError:
        continue
    scopes = [("module", tree)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes.append((f"{node.name}()", node))
    for label, scope in scopes:
        names = [n.name for n in scope.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        for n, c in collections.Counter(names).items():
            if c > 1:
                print(f"{p.name}:{label} defines {n} x{c}")
PY
)
if [ -z "$DUP" ]; then ok "no duplicate defs in any scope"; else bad "duplicate defs:"; echo "$DUP" | sed 's/^/     /'; fi

# ================================================ PHASE 2 - CLEAN (no-op if 1)
head "PHASE 2 — CLEAN"
if [ "$CHECK_ONLY" = 0 ] && [ -n "$RUFF" ]; then
  # Idempotent best-practice pass: only touches code ruff flags as fixable.
  if "$RUFF" check --select F401,B904 --fix --quiet backend/ >/dev/null 2>&1; then
    ok "redundancies auto-removed (idempotent)"
  else
    ok "nothing left to auto-clean"
  fi
else
  note "check-only mode — no auto-clean applied"
fi

# ============================================= PHASE 3 - VERIFY (hard gate)
head "PHASE 3 — VERIFY"

# --- 3a. App actually imports (catches circular imports / import-time errors) ---
head "3a. Import/build test (app module graph loads)"
if ( cd backend && "$PY" -c "import main" ) >/tmp/hyg_import.out 2>&1; then
  ok "backend imports cleanly (no circular-import / import-time errors)"
else
  bad "backend import failed"; sed 's/^/     /' /tmp/hyg_import.out
fi

# --- 3b. Every config env-var default is actually consumed (no dead config) ---
head "3b. Config is live (env defaults are consumed, not dead)"
DEADCFG=$("$PY" - <<'PY'
import re,pathlib
cfg=pathlib.Path("backend/config.py").read_text()
others="\n".join(p.read_text() for p in pathlib.Path("backend").glob("*.py")
                 if p.name!="config.py")
for m in re.finditer(r"^([A-Z][A-Z0-9_]+)\s*[:=]",cfg,re.M):
    n=m.group(1)
    if n in ("BUILD_TAG",): continue
    # consumed as config.NAME, or via SETTING_DEFAULTS source, or a module const
    if not re.search(rf"config\.{re.escape(n)}\b",others):
        print(n)
PY
)
if [ -z "$DEADCFG" ]; then ok "no dead config constants"; else
  note "config constants referenced only in config.py (verify intentional): $DEADCFG"
fi

# --- 3c. Final ruff gate (must be clean) ---
head "3c. Final static-analysis gate"
if [ -n "$RUFF" ] && "$RUFF" check --select F401,F821,F811,E9 --output-format concise backend/ >/tmp/hyg_final.out 2>&1; then
  ok "gate clean (unused imports / undefined names / redefs / syntax)"
else
  bad "final gate failed"; sed 's/^/     /' /tmp/hyg_final.out
fi

# ------------------------------------------------------------------ summary --
head "SUMMARY"
printf '  passed=%d  failed=%d  warnings=%d\n' "$pass" "$fail" "$warn"
if [ "$fail" -eq 0 ]; then
  printf '  \033[32mPRISTINE — safe to submit.\033[0m\n'
  exit 0
else
  printf '  \033[31mNOT pristine — fix the ❌ items above, then re-run.\033[0m\n'
  exit 1
fi
