#!/bin/bash
# Runs every *_test.py in this folder; prints one line per suite.
# A suite that needs the app itself (fastapi/sqlalchemy) is reported SKIP, not FAIL,
# when those aren't installed — so a real failure is never hidden by a bare environment.
cd "$(dirname "$0")/.." || exit 1
fails=0; skips=0
for t in tests/*_test.py tests/*_test.js; do
  [ -e "$t" ] || continue
  case "$t" in
    *.js) name=$(basename "$t" .js); out=$(timeout 300 node "$t" 2>&1); rc=$? ;;
    *)    name=$(basename "$t" .py); out=$(ADOPS_DISABLE_BG=1 PYTHONPATH=. timeout 300 python3 "$t" 2>&1); rc=$? ;;
  esac
  if [ $rc -eq 0 ]; then
    echo "OK   $name"
  elif echo "$out" | grep -q "No module named 'fastapi'\|No module named 'sqlalchemy'"; then
    echo "SKIP $name  (needs the app's dependencies installed)"; skips=$((skips+1))
  else
    echo "FAIL $name"; echo "$out" | grep -E "^FAIL|Error|error:" | head -5; fails=$((fails+1))
  fi
done
echo "---"
[ $skips -gt 0 ] && echo "$skips suite(s) skipped — run these where the app can boot"
[ $fails -eq 0 ] && echo "all suites passed" || echo "$fails suite(s) failed"
exit $fails
