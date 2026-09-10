"""Higgsfield image generation — the public API at https://api.higgsfield.ai.

Contract (docs.higgsfield.ai, OpenAPI 2.0.0, read Sep 2026):
  header  Authorization: Key {HIGGSFIELD_KEY_ID}:{HIGGSFIELD_KEY_SECRET}
  POST    {BASE}{model path}   JSON body (prompt + model-specific fields)
          → {"status": "queued", "request_id", "status_url", "cancel_url"}
  GET     {BASE}/requests/{request_id}/status
          → {"status": queued|in_progress|completed|failed|nsfw|canceled,
             "images": [{"url": ...}], "error": str|null}
  POST    {BASE}/requests/{request_id}/cancel     (only while queued)
  Poll every 2 s, backing off to 10 s. Outputs stay downloadable ≥ 7 days — we
  download them at once. Billing: account credits per SUCCESSFUL request, price
  depends on model + parameters (failed / nsfw requests are not charged).

Image models and the exact fields each accepts (from the OpenAPI spec):
  /nano-banana                     prompt, num_images 1-4, aspect_ratio (auto + 10 ratios),
                                   input_images[≤8] {type:"image_url", image_url}, output_format jpeg|png
  /higgsfield-ai/soul/standard     prompt, num_images 1-4, resolution 2K|4K, aspect_ratio (10 ratios)
  /higgsfield-ai/popcorn/auto      prompt, num_images 1-8, resolution 720p|1600p, aspect_ratio (7),
                                   image_urls[≤8], seed 1-1000000
  /reve/text-to-image              prompt, num_images 1-4
  /reve/edit                       prompt, image_url, num_images 1-4
  /flux-pro/kontext/max/text-to-image  prompt, aspect_ratio (10), seed, safety_tolerance 0-6
The keys are read from env vars only — never stored in code or the DB.
"""
from __future__ import annotations

import time

import httpx

from . import config

BASE = "https://api.higgsfield.ai"
PREFIX = "hf:"                      # ai_model values for Higgsfield rows start with this

ASPECTS_10 = ["1:1", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "16:9", "9:16", "21:9"]

# The docs disagree with themselves about version segments in model paths: the OpenAPI spec
# lists /higgsfield-ai/soul/standard, the quickstart calls /higgsfield-ai/soul/v2/standard.
# A wrong one answers 404 {"detail":"model_not_found"} and costs nothing, so each model carries
# the spec's path plus the version spellings of THE SAME model; the first that isn't a 404 is
# remembered for the rest of the process. Never a different model — only other spellings.
ALIASES: dict[str, list[str]] = {
    "hf:nano-banana": ["/nano-banana", "/nano-banana/v1", "/nano-banana/v2"],
    "hf:soul": ["/higgsfield-ai/soul/standard", "/higgsfield-ai/soul/v2/standard", "/higgsfield-ai/soul/v1/standard"],
    "hf:popcorn": ["/higgsfield-ai/popcorn/auto", "/higgsfield-ai/popcorn/v1/auto", "/higgsfield-ai/popcorn/v2/auto"],
    "hf:reve": ["/reve/text-to-image", "/reve/v1/text-to-image"],
    "hf:reve@edit": ["/reve/edit", "/reve/v1/edit"],
    "hf:flux-kontext": ["/flux-pro/kontext/max/text-to-image", "/flux-pro/v1/kontext/max/text-to-image"],
}
_RESOLVED: dict[str, str] = {}          # model path that answered → used first next time


def candidates(path: str) -> list[str]:
    """Every spelling to try for this endpoint, best first."""
    for key, paths in ALIASES.items():
        if path in paths:
            got = _RESOLVED.get(key)
            return ([got] if got else []) + [p for p in paths if p != got]
    return [path]


def _remember(path: str) -> None:
    for key, paths in ALIASES.items():
        if path in paths:
            _RESOLVED[key] = path


# id -> capabilities (what the form shows) + how to build the request body
MODELS: dict[str, dict] = {
    "hf:nano-banana": {
        "label": "Nano Banana (Higgsfield)", "path": "/nano-banana",
        "aspects": ["auto"] + ASPECTS_10, "max_images": 4, "resolutions": [],
        "edit": True, "max_inputs": 8, "formats": ["jpeg", "png"], "seed": False, "safety": False,
        "note": "Google's Nano Banana run through your Higgsfield credits · edit or create · up to 8 reference images",
    },
    "hf:soul": {
        "label": "Soul (Higgsfield photoreal)", "path": "/higgsfield-ai/soul/standard",
        "aspects": ASPECTS_10, "max_images": 4, "resolutions": ["2K", "4K"],
        "edit": False, "max_inputs": 0, "formats": [], "seed": False, "safety": False,
        "note": "Higgsfield's own photoreal model · text → image only · 2K or 4K",
    },
    "hf:popcorn": {
        "label": "Popcorn (product in scene)", "path": "/higgsfield-ai/popcorn/auto",
        "aspects": ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"], "max_images": 8, "resolutions": ["720p", "1600p"],
        "edit": True, "max_inputs": 8, "formats": [], "seed": True, "safety": False,
        "note": "Up to 8 reference images and 8 outputs per run · seed for repeatable results",
    },
    "hf:reve": {
        "label": "Reve", "path": "/reve/text-to-image", "edit_path": "/reve/edit",
        "aspects": [], "max_images": 4, "resolutions": [],
        "edit": True, "max_inputs": 1, "formats": [], "seed": False, "safety": False,
        "note": "Fast and literal with text in the image · create, or edit one source image",
    },
    "hf:flux-kontext": {
        "label": "Flux Kontext Max", "path": "/flux-pro/kontext/max/text-to-image",
        "aspects": ASPECTS_10, "max_images": 1, "resolutions": [],
        "edit": False, "max_inputs": 0, "formats": [], "seed": True, "safety": True,
        "note": "Text → image, one per run · seed + safety tolerance (0 strict … 6 loose)",
    },
}
DEFAULT_MODEL = "hf:nano-banana"
TERMINAL = ("completed", "failed", "nsfw", "canceled")


class HiggsfieldError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message, self.code = message, code


def configured() -> bool:
    return bool(config.HIGGSFIELD_KEY_ID and config.HIGGSFIELD_KEY_SECRET)


def is_hf(model: str | None) -> bool:
    return bool(model) and str(model).startswith(PREFIX)


def _headers() -> dict:
    return {"Authorization": f"Key {config.HIGGSFIELD_KEY_ID}:{config.HIGGSFIELD_KEY_SECRET}",
            "Content-Type": "application/json"}


def _explain(status: int, body: str) -> str:
    """Turn an HTTP error into the sentence an operator needs."""
    if status == 401:
        return "Higgsfield rejected the API key — check HIGGSFIELD_KEY_ID / HIGGSFIELD_KEY_SECRET on Render."
    if status in (402, 403):
        return "Higgsfield refused the request (no credits, or the key has no access to this model) — top up credits at higgsfield.ai."
    if status == 422:
        return f"Higgsfield rejected a setting: {body[:200]}"
    if status == 429:
        return "Higgsfield rate limit hit — wait a minute and Retry."
    return f"Higgsfield HTTP {status}: {body[:200]}"


def build_body(model: str, prompt: str, *, num_images: int = 1, aspect: str = "", resolution: str = "",
               input_urls: list[str] | None = None, output_format: str = "", seed: int | None = None,
               safety_tolerance: int | None = None) -> tuple[str, dict]:
    """(endpoint path, JSON body) for the model — only fields the spec lists, only when set."""
    spec = MODELS.get(model)
    if not spec:
        raise HiggsfieldError(f"unknown Higgsfield model {model}", "model")
    urls = [u for u in (input_urls or []) if u]
    if urls and not spec["edit"]:
        raise HiggsfieldError(f"{spec['label']} creates from text only — it can't take a source image", "no_edit")
    urls = urls[: spec["max_inputs"]]
    n = max(1, min(int(num_images or 1), spec["max_images"]))
    body: dict = {"prompt": prompt.strip()}
    path = spec["path"]
    if model == "hf:nano-banana":
        body["num_images"] = n
        if aspect in spec["aspects"]:
            body["aspect_ratio"] = aspect
        if urls:
            body["input_images"] = [{"type": "image_url", "image_url": u} for u in urls]
        body["output_format"] = output_format if output_format in spec["formats"] else "jpeg"
    elif model == "hf:soul":
        body["num_images"] = n
        body["resolution"] = resolution if resolution in spec["resolutions"] else "2K"
        if aspect in spec["aspects"]:
            body["aspect_ratio"] = aspect
    elif model == "hf:popcorn":
        body["num_images"] = n
        body["resolution"] = resolution if resolution in spec["resolutions"] else "720p"
        if aspect in spec["aspects"]:
            body["aspect_ratio"] = aspect
        if urls:
            body["image_urls"] = urls
        if seed:
            body["seed"] = max(1, min(int(seed), 1_000_000))
    elif model == "hf:reve":
        body["num_images"] = n
        if urls:
            path = spec["edit_path"]
            body["image_url"] = urls[0]
    elif model == "hf:flux-kontext":
        if aspect in spec["aspects"]:
            body["aspect_ratio"] = aspect
        if seed:
            body["seed"] = max(1, min(int(seed), 1_000_000))
        if safety_tolerance is not None:
            body["safety_tolerance"] = max(0, min(int(safety_tolerance), 6))
    return path, body


def submit(model: str, prompt: str, timeout: float = 60.0, **opts) -> dict:
    """Queue a generation. Returns the request record ({request_id, status_url, status}).
    A 404 model_not_found means that spelling of the endpoint isn't the one this account is
    served — the other documented spellings are tried before giving up (a 404 is free)."""
    if not configured():
        raise HiggsfieldError("HIGGSFIELD_KEY_ID / HIGGSFIELD_KEY_SECRET are not set", "not_configured")
    path, body = build_body(model, prompt, **opts)
    tried: list[str] = []
    for cand in candidates(path):
        tried.append(cand)
        try:
            r = httpx.post(BASE + cand, json=body, headers=_headers(), timeout=timeout)
        except httpx.HTTPError as e:
            raise HiggsfieldError(f"network error reaching Higgsfield: {e}", "network") from e
        if r.status_code == 404 and "model_not_found" in r.text:
            continue
        if r.status_code >= 400:
            raise HiggsfieldError(_explain(r.status_code, r.text), str(r.status_code))
        data = r.json()
        if not data.get("request_id"):
            raise HiggsfieldError(f"Higgsfield answered without a request id: {r.text[:200]}", "api")
        _remember(cand)
        return data
    label = MODELS.get(model, {}).get("label", model)
    raise HiggsfieldError(f"Higgsfield has no “{label}” on this account (model_not_found). Pick another model in the list, "
                          f"or check which models your Higgsfield API plan includes at higgsfield.ai. Tried: {', '.join(tried)}",
                          "model_not_found")


def status(request_id: str, timeout: float = 30.0) -> dict:
    try:
        r = httpx.get(f"{BASE}/requests/{request_id}/status", headers=_headers(), timeout=timeout)
    except httpx.HTTPError as e:
        raise HiggsfieldError(f"network error reaching Higgsfield: {e}", "network") from e
    if r.status_code >= 400:
        raise HiggsfieldError(_explain(r.status_code, r.text), str(r.status_code))
    return r.json()


def cancel(request_id: str, timeout: float = 30.0) -> bool:
    try:
        r = httpx.post(f"{BASE}/requests/{request_id}/cancel", headers=_headers(), timeout=timeout)
    except httpx.HTTPError:
        return False
    return r.status_code < 400


def wait(request_id: str, *, max_wait_s: float = 900.0, on_stage=None, should_stop=None) -> dict:
    """Poll until a terminal state (2 s → 10 s back-off, as the docs recommend).
    Returns the final status record; raises on timeout."""
    delay, waited = 2.0, 0.0
    last_stage = ""
    while True:
        if should_stop and should_stop():
            raise HiggsfieldError("stopped", "stopped")
        try:
            d = status(request_id)
        except HiggsfieldError as e:
            if e.code == "network":          # one flaky poll must not fail a paid render
                d = {"status": last_stage or "queued"}
            else:
                raise
        st = d.get("status") or ""
        if st != last_stage and on_stage:
            on_stage(st)
        last_stage = st
        if st in TERMINAL:
            return d
        if waited >= max_wait_s:
            raise HiggsfieldError(f"still not finished after {int(max_wait_s // 60)} min — Higgsfield is slow right now; press Retry later", "timeout")
        time.sleep(delay)
        waited += delay
        delay = min(10.0, delay * 1.5)


def download(url: str, dst: str, timeout: float = 300.0) -> str:
    """Save an output image; returns the mime type Higgsfield sent."""
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
            if r.status_code >= 400:
                raise HiggsfieldError(f"couldn't download the result (HTTP {r.status_code})", "download")
            mime = (r.headers.get("content-type") or "").split(";")[0].strip()
            with open(dst, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
            return mime
    except httpx.HTTPError as e:
        raise HiggsfieldError(f"couldn't download the result: {e}", "download") from e
