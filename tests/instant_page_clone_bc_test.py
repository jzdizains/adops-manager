"""Instant Pages: clone a page to every account of a Business Center (v120).

- the page computes, per page name and BC, how many accounts still lack it
- POST /instant-pages/clone-bc targets only enabled accounts of that BC, in this view,
  that don't already hold the name, and runs as a (slow-lane) job
- the job clones one account at a time, re-reads each target, pauces, stops on request,
  and reports per-account failures without stopping the rest
- the popover offers the BC option with the live count and a confirm
Static checks only (the route needs the app to boot)."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
r = read("app/routes/instant_pages.py"); t = read("app/templates/instant_pages.html"); h = read("app/job_handlers.py"); j = read("app/jobs.py")
check("page computes missing-per-BC", "missing = {name: {bc: sum(1 for aid in ids if aid not in have.get(name, set()))" in r and '"bcs": bcs, "missing": missing' in r)
check("route: enabled, in this BC, in view, not the source, not already holding the name", 'a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have' in r)
check("route needs the web cookies and queues a job", 'if not spark_web_api.load_cookies():' in r.split("def clone_bc")[1][:600] and 'jobs.enqueue(db, "instant_page_clone_all"' in r)
check("job body: per-account copy through the page editor API, verified by re-read, pause, stop check", "r = clone_one(db, page_id, name, acct, new_url=new_url, new_text=new_text, source_owner=from_advertiser_id)" in r and "if should_stop and should_stop():" in r and "_time.sleep(1.5)" in r and "sync_account(db, acct)" in r.split("def clone_one")[1])
check("handler registered in the slow lane with progress", '@jobs.handler("instant_page_clone_all")' in h and "on_progress=lambda t: jobs.progress(db, job, t)" in h.split('@jobs.handler("instant_page_clone_all")')[1][:900] and '"instant_page_clone_all"' in j.split("SLOW_KINDS = {")[1][:80])
check("popover: BC select with live count, confirm, submits the BC form", 'class="ip-bc-form"' in t and 'data-n="{{ n }}"' in t and "Clone to all (" in t and "UI.confirm({ title: \"Clone “\" + b.dataset.name" in t and "fb.submit()" in t)
print(); print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"); sys.exit(1 if fails else 0)
