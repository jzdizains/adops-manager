#!/bin/bash
# Runs every *_test.py in this folder; prints one line per suite.
cd "$(dirname "$0")/.." || exit 1
fails=0
for t in tests/*_test.py; do
  name=$(basename "$t" .py)
  out=$(ADOPS_DISABLE_BG=1 PYTHONPATH=. timeout 300 python3 "$t" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then echo "OK   $name"; else
    echo "FAIL $name"; echo "$out" | grep -E "^FAIL|Error|error:" | head -5; fails=$((fails+1))
  fi
done
echo "---"; [ $fails -eq 0 ] && echo "all suites passed" || echo "$fails suite(s) failed"
exit $fails
