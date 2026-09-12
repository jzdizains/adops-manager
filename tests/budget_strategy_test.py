"""CBO / ABO — the campaign budget strategy in the preset builder.

The control was never removed, but it sat in step 4 while TikTok Ads Manager puts
"Budget strategy" on the campaign step — so it read as missing. It now lives in step 1
with the rest of the campaign settings. That is a layout change, and the thing worth
pinning is that it still SAVES the same way: ABO must clear the campaign budget, and CBO
must keep it, because that single field decides whether TikTok gets a campaign-level
budget or a per-ad-group one.
"""
import sys, os, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)

form = open(os.path.join(ROOT, "app", "templates", "template_form.html")).read()

def section(tab: str) -> str:
    """The <section> for a step — not the tab BUTTON that carries the same data-tab."""
    m = re.search(r'<section[^>]*data-tab="%s"' % re.escape(tab), form)
    assert m, f"no section for {tab}"
    j = form.find("</section>", m.start())
    return form[m.start():j if j > 0 else len(form)]

campaign, budget = section("campaign"), section("budget")

print("\n-- the choice is on the campaign step, where TikTok has it --")
check("the strategy cards are in step 1", 'id="budgetCards"' in campaign)
check("all three strategies are offered",
      all(f'data-val="{v}"' in campaign for v in ("ABO", "BUDGET_MODE_DAY", "BUDGET_MODE_TOTAL")))
check("it is labelled the way Ads Manager labels it", "Budget strategy" in campaign)
check("the field that gets saved is with them", 'name="campaign_budget_mode"' in campaign)

print("\n-- and only once: two copies would be two answers --")
check("no second card grid", form.count('id="budgetCards"') == 1, str(form.count('id="budgetCards"')))
check("no second select", form.count('name="campaign_budget_mode"') == 1,
      str(form.count('name="campaign_budget_mode"')))

print("\n-- step 4 keeps the amounts and points back --")
check("campaign budget amount still there", 'name="campaign_budget"' in budget)
check("ad group budget amount still there", 'name="adgroup_budget"' in budget)
check("it echoes the chosen strategy", 'id="pbStrategyEcho"' in budget)
check("with a way back to step 1", 'data-next="campaign"' in budget)

print("\n-- the saved values still mean what they meant --")
src = open(os.path.join(ROOT, "app", "routes", "templates_routes.py")).read()
check("ABO clears the campaign budget",
      re.search(r'if top\["campaign_budget_mode"\] == "ABO":\s*\n\s*top\["campaign_budget"\] = None', src) is not None)
check("the default is ABO", 'val("campaign_budget_mode", "ABO")' in src)

print("\n-- the builder still drives the right fields --")
js = open(os.path.join(ROOT, "app", "static", "preset-builder.js")).read()
check("the cards are bound to the select", 'bindCards("budgetCards", "budgetMode"' in js)
check("ABO shows the ad group amount", '$("#aboBudgetField").hidden = !abo' in js)
check("CBO shows the campaign amount", '$("#cboBudgetField").hidden = abo' in js)
check("every strategy has a readable label",
      all(k in js for k in ("ABO:", "BUDGET_MODE_DAY:", "BUDGET_MODE_TOTAL:")))

print()
print(("FAILED: " + ", ".join(fails)) if fails else "all good")
sys.exit(1 if fails else 0)
