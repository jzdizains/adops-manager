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
r = read("app/routes/instant_pages.py"); t = read("app/templates/instant_pages.html"); h = read("app/job_handlers.py"); j = read("app/jobs.py"); v = read("app/instant_pages_view.py"); js = read("app/static/instant-pages.js")
check("page computes missing-per-BC (v132: per page name, in instant_pages_view)", 'missing_by_bc = {bc: sum(1 for aid in ids if aid not in g["have"]) for bc, ids in by_bc.items()}' in v and '"missing_by_bc": missing_by_bc' in v and "ipv.group_pages(pages, accounts, bc_names" in r)
check("route: enabled, in this BC, in view, not the source, not already holding the name", 'a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have' in r)
check("route needs the web cookies and queues a job", 'if not spark_web_api.load_cookies():' in r.split("def clone_bc")[1][:600] and 'jobs.enqueue(db, "instant_page_clone_all"' in r)
check("job body: per-account copy through the page editor API, verified by re-read, pause, stop check", "r = clone_one(db, page_id, name, acct, new_url=new_url, new_text=new_text, source_owner=from_advertiser_id)" in r and "if should_stop and should_stop():" in r and "_time.sleep(1.5)" in r and "sync_account(db, acct)" in r.split("def clone_one")[1])
check("handler registered in the slow lane with progress", '@jobs.handler("instant_page_clone_all")' in h and "on_progress=lambda t: jobs.progress(db, job, t)" in h.split('@jobs.handler("instant_page_clone_all")')[1][:900] and '"instant_page_clone_all"' in j.split("SLOW_KINDS = {")[1][:80])
check("popover (v132, built from the page JSON): BC select with live count, confirm, posts the BC form", "data-n=\"' + x.missing" in js and "Clone to all (" in js and 'UI.confirm({ title: "Clone “" + g.name' in js and 'postForm("/instant-pages/clone-bc"' in js and 'class="ip-bc-form"' not in t)
print(); print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"); sys.exit(1 if fails else 0)
