"""Navigation model for the shell: the 10-item sidebar, the section tabs shown
in the top bar (siblings of the current page), and the ⌘K jump list.

Everything the old 25-item sidebar linked to is still reachable — grouped
pages became tabs of their section, and every page is in the ⌘K list."""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

# ---- sidebar (href, icon, label, match-prefixes) ------------------------------
SIDEBAR = [
    ("/", "home", "Home", ("/",)),
    ("/status", "campaigns", "Campaigns", ("/status", "/campaigns/*")),
    ("/pnl", "pnl", "P&L", ("/pnl",)),
    ("/monitor", "health", "Health", ("/monitor", "/issues", "/appeals", "/automation")),
    ("/audience", "audience", "Audience", ("/audience",)),
    ("/inbox", "inbox", "Inbox", ("/inbox",)),
    None,                                                   # gap
    ("/super-launcher", "rocket", "Launch", ("/super-launcher", "/campaigns/launch", "/campaigns/result", "/queue")),
    ("/presets", "presets", "Presets", ("/presets",)),
    ("/creatives", "creatives", "Creatives", ("/creatives", "/spark-codes", "/ad-texts", "/instant-pages", "/lead-forms", "/display-cards")),
]
FOOTER = [
    ("/jobs", "jobs", "Jobs", ("/jobs",)),
    ("/settings", "settings", "Settings", ("/settings", "/accounts", "/pixels", "/partners", "/locations", "/cookies", "/oauth", "/escape-test", "/campaigns/source-check")),
]

# ---- section tabs: (href, label) — 'on' is decided by path + query -----------
SECTIONS = {
    "health": [("/monitor", "Monitor"), ("/monitor?view=balances", "Balances"), ("/monitor?view=issues", "Issues"),
               ("/appeals", "Appeals"), ("/automation", "Automation")],
    "launch": [("/super-launcher", "Super Launcher"), ("/campaigns/launch", "Single campaign"), ("/queue", "Queue")],
    "creatives": [("/creatives", "Library"), ("/creatives?view=performance", "Performance"), ("/spark-codes", "Spark codes"),
                  ("/ad-texts", "Ad texts"), ("/instant-pages", "Instant pages"), ("/lead-forms", "Lead forms")],
    "settings": [("/settings", "General"), ("/settings#security", "Security"), ("/settings#users", "Users"), ("/settings#access", "Access log"),
                 ("/accounts", "Ad accounts"), ("/pixels", "Pixels"), ("/partners", "Partners"), ("/locations", "Locations"),
                 ("/cookies", "Cookies")],
    "tools": [("/campaigns/source-check", "Source check"), ("/escape-test", "Escape test")],
}
_SECTION_OF = {
    "/monitor": "health", "/issues": "health", "/appeals": "health", "/automation": "health",
    "/super-launcher": "launch", "/campaigns/launch": "launch", "/queue": "launch", "/campaigns/result": "launch",
    "/creatives": "creatives", "/spark-codes": "creatives", "/ad-texts": "creatives", "/instant-pages": "creatives", "/lead-forms": "creatives",
    "/settings": "settings", "/accounts": "settings", "/pixels": "settings", "/partners": "settings", "/locations": "settings",
    "/cookies": "settings", "/oauth": "settings",
    "/campaigns/source-check": "tools", "/escape-test": "tools",
}

# ---- ⌘K jump list: (href, label, group, keywords) -----------------------------
JUMP = [
    ("/", "Home", "Monitor", "overview dashboard today"),
    ("/status", "Campaigns", "Monitor", "ads manager table spend roas"),
    ("/pnl", "P&L", "Monitor", "profit revenue postbacks sources"),
    ("/monitor", "Health", "Monitor", "cookies system balances"),
    ("/monitor?view=balances", "Balances", "Monitor", "business center wallet top up"),
    ("/monitor?view=issues", "Issues", "Monitor", "rejected ads blocked accounts"),
    ("/appeals", "Appeals", "Monitor", "appeal rejected ads"),
    ("/automation", "Automation", "Monitor", "rules auto pause paused campaigns resume"),
    ("/audience", "Audience", "Monitor", "hours weekday heatmap countries"),
    ("/inbox", "Inbox", "Monitor", "alerts notifications attention"),
    ("/super-launcher", "Super Launcher", "Launch", "launch many accounts preset"),
    ("/campaigns/launch", "Create campaign", "Launch", "single campaign launch one account"),
    ("/queue", "Launch queue", "Launch", "queued scheduled launches"),
    ("/presets", "Presets", "Build", "templates campaign settings"),
    ("/presets/new", "New preset", "Build", "create template"),
    ("/creatives", "Creatives", "Build", "videos images carousels library upload"),
    ("/creatives?view=performance", "Creative performance", "Build", "best creatives leaderboard roas"),
    ("/spark-codes", "Spark codes", "Build", "creator posts authorization"),
    ("/ad-texts", "Ad texts", "Build", "copy captions"),
    ("/instant-pages", "Instant pages", "Build", "landing pages tiktok"),
    ("/lead-forms", "Lead forms", "Build", "lead generation forms"),
    ("/jobs", "Jobs", "System", "background tasks running"),
    ("/settings", "Settings", "System", "postback pixel events api defaults"),
    ("/settings#security", "Security", "System", "2fa password sessions"),
    ("/settings#users", "Users", "System", "team members invite"),
    ("/settings#access", "Access log", "System", "logins ip location device"),
    ("/accounts", "Ad accounts", "System", "advertisers enable disable"),
    ("/pixels", "Pixels", "System", "tracking pixel events"),
    ("/partners", "Partners", "System", "business center partner share assets"),
    ("/locations", "Locations", "System", "geo targeting countries regions"),
    ("/cookies", "TikTok cookies", "System", "session cookies browser"),
    ("/oauth/connect", "Connect TikTok", "System", "oauth authorize token"),
    ("/campaigns/source-check", "Source check", "Tools", "verify postback source lander"),
    ("/escape-test", "Escape test", "Tools", "phone landing test"),
]


def _matches(item_prefixes, path: str) -> bool:
    for pre in item_prefixes:
        if pre == "/":
            if path == "/":
                return True
        elif pre == "/campaigns/*":      # campaign edit/bids pages, not the launch/result/tool pages
            if path.startswith("/campaigns/") and section_key(path) is None:
                return True
        elif path == pre or path.startswith(pre.rstrip("/") + "/") or path.startswith(pre):
            return True
    return False


def sidebar(path: str) -> list:
    """[(href, icon, label, active) | None] for the main nav."""
    out = []
    for it in SIDEBAR:
        if it is None:
            out.append(None)
            continue
        href, icon, label, pres = it
        out.append((href, icon, label, _matches(pres, path)))
    return out


def footer(path: str) -> list:
    return [(h, i, l, _matches(p, path)) for h, i, l, p in FOOTER]


def _tab_on(tab_href: str, path: str, query: dict, fragment_ok: bool) -> bool:
    u = urlparse(tab_href)
    if u.path != path:
        return False
    want = parse_qs(u.query)
    if want:
        return all(query.get(k, [""])[0] == v[0] for k, v in want.items())
    if u.fragment:
        return False                     # anchor tabs are never 'on' server-side (JS marks them)
    # a tab without a query is on only when none of its sibling query-tabs match
    return not any(parse_qs(urlparse(h).query) and all(query.get(k, [""])[0] == v[0] for k, v in parse_qs(urlparse(h).query).items())
                   for h, _ in SECTIONS.get(section_key(path), []) if urlparse(h).path == path)


def section_key(path: str) -> str | None:
    for pre, key in sorted(_SECTION_OF.items(), key=lambda kv: -len(kv[0])):
        if path == pre or path.startswith(pre + "/") or path.startswith(pre + "?"):
            return key
    return None


def tabs(request) -> list:
    """[(href, label, on)] for the current page's section, [] when it has none."""
    path = request.url.path
    key = section_key(path)
    if not key:
        return []
    query = parse_qs(request.url.query)
    return [(h, l, _tab_on(h, path, query, False)) for h, l in SECTIONS[key]]
