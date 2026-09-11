# Tests

Everything in here runs against the app in-process (FastAPI TestClient) or against a
local uvicorn + Chromium (Playwright) — nothing touches TikTok, Higgsfield or Glitchy.

    bash tests/run_all.sh            # every suite, one line each
    python3 tests/status_range_test.py    # one suite

Requirements: the app's own requirements plus `playwright` with Chromium
(`PW=/opt/pw-browsers/chromium` in the sandbox). Suites that need a browser skip
themselves when Chromium isn't there.

They live in the repo on purpose: a test that only exists on someone's scratch
disk is gone the moment that disk is.
