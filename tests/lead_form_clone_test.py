"""Lead Forms: clone an instant form to one account or to every account of a Business
Center (v120). An instant form is a TikTok page with business_type LEAD_GEN (that is how
/page/get/ lists it — tiktok_api.list_all_lead_forms), so it rides the same web page-copy
call as an Instant Page; every copy is VERIFIED by re-reading the target account.
Static checks only (the routes need the app to boot)."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()
r = read("app/routes/lead_forms.py"); t = read("app/templates/lead_forms.html"); h = read("app/job_handlers.py"); j = read("app/jobs.py")
check("lead forms are LEAD_GEN pages", 'list_all_pages(access_token, advertiser_id, ["LEAD_GEN"])' in read("app/tiktok_api.py"))
check("sync split into a per-account helper the clone can re-read with", "def sync_account(db: Session, acct: models.AdAccount) -> int:" in r and "synced += sync_account(db, acct)" in r)
check("every copy is verified by re-reading the target and looking for the name", "exists = db.query(models.LeadForm).filter_by(owner_advertiser_id=acct.advertiser_id, name=name).first() is not None" in r and "no form with this name appeared" in r)
check("single clone route + BC route, both need the web cookies and the view", r.count("if not spark_web_api.load_cookies():") == 4 and '@router.post("/lead-forms/clone")' in r and '@router.post("/lead-forms/clone-bc")' in r and r.count("sc.allows(") >= 4)  # +2: read-only /lead-forms/inspect and /lead-forms/build also guard cookies
check("BC route targets: enabled, this BC, in view, not the source, not already holding the name", 'a.owner_bc_id == bc_id and sc.allows(a.advertiser_id) and a.advertiser_id != from_advertiser_id and a.advertiser_id not in have' in r)
check("runs as a slow-lane job with progress + stop; one failure never stops the rest", '@jobs.handler("lead_form_clone_all")' in h and '"lead_form_clone_all"' in j.split("SLOW_KINDS = {")[1][:120] and "if should_stop and should_stop():" in r.split("def clone_to_many")[1] and "_time.sleep(1.5)" in r)
check("page computes copies + missing-per-BC", '"copies": copies, "bcs": bcs, "missing": missing' in r)
check("popover: single account + BC option with live count and confirm", 'class="lf-bc-form"' in t and 'data-n="{{ n }}"' in t and "Clone to all (" in t and "fb.submit()" in t and 'action="/lead-forms/clone"' in t)
print(); print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}"); sys.exit(1 if fails else 0)
