"""Every `models.<Name>` the app references must be a class defined in app/models.py.

The other suites stub `app.models`, so a model that is referenced but never defined
(v106 shipped adgroup_stats without AdgroupSnapshot after a failed edit) passes every
test and only explodes on the live page. This is the static check that would have
caught it. Runs without any dependency."""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

models_src = open(os.path.join(APP, "models.py"), encoding="utf-8").read()
defined = set(re.findall(r"^class (\w+)\(", models_src, re.M))
defined |= set(re.findall(r"^(\w+)\s*=", models_src, re.M))          # module-level names (utcnow, Base…)
check("models.py defines classes", len(defined) > 20)

ref_re = re.compile(r"\bmodels\.([A-Z]\w+)")
missing = {}
for dirpath, _dirs, files in os.walk(APP):
    for fn in files:
        if not fn.endswith(".py") or fn == "models.py":
            continue
        path = os.path.join(dirpath, fn)
        src = open(path, encoding="utf-8").read()
        for name in set(ref_re.findall(src)):
            if name not in defined:
                missing.setdefault(name, []).append(os.path.relpath(path, ROOT))
check("every models.<Name> referenced in app/ exists", not missing, str(missing))

# the ones this build depends on, by name
for name in ("AdgroupSnapshot", "CampaignPref", "Tag", "CampaignTag", "LanderEvent", "Appeal", "PostbackEvent"):
    check(f"models.{name} is defined", name in defined)

# columns the new code reads must exist on their models
def has_col(model, col):
    m = re.search(rf"^class {model}\(.*?(?=^class |\Z)", models_src, re.M | re.S)
    return bool(m) and re.search(rf"^\s+{col}\s*=\s*Column\(", m.group(0), re.M) is not None
for model, col in (("PostbackEvent", "adgroup_id"), ("LanderEvent", "via"), ("AdgroupSnapshot", "operation_status"),
                   ("AdgroupSnapshot", "created_time"), ("CampaignPref", "ag_active_only"), ("Tag", "color"), ("CampaignTag", "tag_id")):
    check(f"{model}.{col} column exists", has_col(model, col))

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
