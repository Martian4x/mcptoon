#!/usr/bin/env bash
# cleanroom_check.sh — prove the SHIPPED artifact works, on more than one Python.
#
# Why this exists, and how it differs from scripts/cleanroom/:
#   * scripts/cleanroom/ (Tier 1 + Tier 2) runs the lifecycle suite against a
#     *source* (editable) install, on Python 3.12 only. That is the right tool for
#     "did my path/agent logic break" — but it cannot catch two classes of bug that
#     have actually shipped before:
#       1. the wheel is missing a file the source tree has (package_data / MANIFEST
#          drift) — editable install sees the tree, the wheel does not;
#       2. a change works on the dev's 3.12 but breaks the minimum supported 3.10.
#   * This script closes exactly those: it builds the real wheel from the current
#     tree, installs THAT into a clean container (no source tree on the path), and
#     runs a small smoke suite across every Python version in $PY_VERSIONS.
#
# It is deliberately small and assertion-based: exit 0 = every assertion held on
# every version; exit 1 = something failed (the failing line is named). It needs
# Docker; it does not need network beyond the base image + pip (stdlib only, so no
# third-party wheels are pulled).
#
# Usage:
#   bash scripts/cleanroom_check.sh                 # 3.10 + 3.12
#   PY_VERSIONS="3.10 3.11 3.12" bash scripts/cleanroom_check.sh
#   bash scripts/cleanroom_check.sh --keep          # keep the temp build dir
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY_VERSIONS="${PY_VERSIONS:-3.10 3.12}"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
fail() { printf '\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "docker not on PATH — this check needs it"

WORK="$(mktemp -d)"
cleanup() { [[ "$KEEP" == "1" ]] || rm -rf "$WORK"; }
trap cleanup EXIT

say "Building the wheel from the current tree"
python -m pip wheel . --no-deps -w "$WORK/dist" >/dev/null
WHEEL="$(ls "$WORK"/dist/mcptoon-*.whl | head -1)"
[[ -f "$WHEEL" ]] || fail "no wheel produced"
echo "  built: $(basename "$WHEEL")"

# A self-contained smoke suite. It runs INSIDE the container against the installed
# wheel (no repo on sys.path), and asserts the presence/discoverability surface
# this project keeps getting wrong: the badge is one clean line, it is bilingual,
# the onboarding surfaces point at it, uninstall says what you lose, doctor reports
# the badge, and savings derives from the same estimate as the footer.
cat > "$WORK/smoke.py" <<'PY'
import io, json, subprocess, sys, os

failures = []
def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        failures.append(name)

def run(args, env=None):
    e = dict(os.environ); e.update(env or {})
    return subprocess.run([sys.executable, "-m", "mcptoon", *args],
                          capture_output=True, text=True, env=e)

# 1. version is importable and the package is the installed one, not a source tree.
r = run(["--version"])
check("version prints", r.returncode == 0 and "mcptoon" in r.stdout, r.stdout + r.stderr)

# 2. statusline: exactly one line on stdout, empty stderr (the footer-silent fix).
r = run(["statusline"], env={"MCPTOON_LANG": "en"})
out_lines = [l for l in r.stdout.splitlines() if l.strip()]
check("statusline one line", len(out_lines) == 1, repr(r.stdout))
check("statusline stderr empty", r.stderr == "", repr(r.stderr))
check("statusline has the mark", "\U0001F389" in r.stdout or "mcptoon" in r.stdout)

# 3. bilingual: zh vs en differ, both carry the mark.
zh = run(["statusline"], env={"MCPTOON_LANG": "zh"}).stdout
en = run(["statusline"], env={"MCPTOON_LANG": "en"}).stdout
check("statusline bilingual", zh.strip() != en.strip(), f"zh={zh!r} en={en!r}")

# 4. savings command exists and derives a number (or says none yet) without crashing.
r = run(["savings"], env={"MCPTOON_LANG": "en"})
check("savings runs", r.returncode == 0, r.stderr)
check("savings mentions milestones or none", ("milestone" in r.stdout.lower() or "No savings" in r.stdout), r.stdout)

# 5. uninstall --dry names what you lose (the history summary) OR says nothing to remove.
r = run(["uninstall", "--dry"], env={"MCPTOON_LANG": "en"})
check("uninstall dry runs", r.returncode == 0, r.stderr)
check("uninstall dry is a plan", "This will" in r.stdout or "nothing to remove" in r.stdout, r.stdout)

# 6. statusline --guide shows manual wiring for non-Claude hosts.
r = run(["statusline", "--guide"], env={"MCPTOON_LANG": "en"})
check("guide shows tmux + shell", "status-right" in r.stdout and "PS1" in r.stdout, r.stdout)

# 7. serve instructions carry the presence receipt and stay within budget.
from mcptoon import serve
t = serve._INSTRUCTIONS
check("instructions within budget", len(t) <= 1400, f"len={len(t)}")
check("instructions name statusline", "mcptoon statusline" in t)
check("instructions name presence receipt", "presence receipt" in t)

# 8. savings total equals the footer cumulative estimate (one number, two screens).
from mcptoon import footer, savings
facts = footer.facts()
est = footer._cumulative_estimate(facts)
expected = est[1] if est else 0
check("savings == footer estimate", savings.build()["total_tokens"] == expected)

print()
if failures:
    print(f"FAILED {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ALL SMOKE CHECKS PASSED")
PY

overall=0
for VER in $PY_VERSIONS; do
  say "Python $VER — install the wheel into a clean container and smoke-test it"
  IMG="mcptoon-wheel-smoke:${VER}"
  cat > "$WORK/Dockerfile" <<EOF
FROM python:$VER-slim
COPY $(basename "$WHEEL") /tmp/$(basename "$WHEEL")
RUN pip install --quiet --no-cache-dir /tmp/$(basename "$WHEEL")
COPY smoke.py /smoke.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "/smoke.py"]
EOF
  cp "$WHEEL" "$WORK/"
  if ! docker build -q -t "$IMG" "$WORK" >/dev/null; then
    fail "docker build failed for python $VER"
  fi
  if docker run --rm "$IMG"; then
    echo "  -> Python $VER PASSED"
  else
    echo "  -> Python $VER FAILED"; overall=1
  fi
done

say "Result"
if [[ "$overall" == "0" ]]; then
  echo "cleanroom_check: PASS on: $PY_VERSIONS"
else
  echo "cleanroom_check: FAIL (see above)"; exit 1
fi
