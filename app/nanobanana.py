"""Gemini image generation / editing ("Nano Banana") — Interactions API.

Contract (ai.google.dev/gemini-api/docs/image-generation + /api/interactions-api):
  POST https://generativelanguage.googleapis.com/v1beta/interactions
  header  x-goog-api-key: <GEMINI_API_KEY>
  body    {"model": "...",
           "input": [{"type":"text","text": prompt},
                     {"type":"image","mime_type":"image/png","data": <base64>}],   # omit for text→image
           "response_format": {"type":"image","mime_type":"image/jpeg","image_size":"1K"}}   # jpeg is the only accepted output
  reply   {"status":"completed", "steps":[{"type":"model_output",
             "content":[{"type":"image","mime_type":"image/png","data":<base64>}, ...]}], "usage":{...}}
The generated image is the LAST image block across steps[].content[].

Pricing (ai.google.dev/gemini-api/docs/pricing, standard tier, per image):
  gemini-3.1-flash-lite-image  $0.0336 (1K only)
  gemini-3.1-flash-image       $0.045 512 · $0.067 1K · $0.101 2K · $0.151 4K
  gemini-3-pro-image           $0.134 1K/2K · $0.24 4K
The key is read from the GEMINI_API_KEY env var only — never stored in code or DB.
"""
from __future__ import annotations

import base64

import httpx

from . import config

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

# id -> (label, {image_size: usd})
MODELS: dict[str, tuple[str, dict[str, float]]] = {
    "gemini-3.1-flash-lite-image": ("Nano Banana 2 Lite — fastest, cheapest", {"1K": 0.0336}),
    "gemini-3.1-flash-image":      ("Nano Banana 2 — best all-rounder",
                                    {"512": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151}),   # API enum: "512" (not 0.5K)
    "gemini-3-pro-image":          ("Nano Banana Pro — highest quality",
                                    {"1K": 0.134, "2K": 0.134, "4K": 0.24}),
}
DEFAULT_MODEL = "gemini-3.1-flash-image"
DEFAULT_SIZE = "1K"
ASPECTS = ["1:1", "9:16", "16:9", "4:5", "3:4", "4:3", "2:3", "3:2", "21:9"]

# Without this the model sometimes answers a prompt with a paragraph instead of a
# picture ("This image contains metadata. While I cannot directly access…") — an
# ad-creative editor must always hand back an image.
SYSTEM_EDIT = ("You are an image editor for advertising creatives. The user gives you an image and "
               "an instruction. ALWAYS return exactly one edited image and no text. Apply the instruction "
               "as a visual change to the picture; keep everything not mentioned (product, people, layout, "
               "text on the image) exactly as it is. If the instruction is not a visual change or is "
               "unclear, still return the image with the closest reasonable visual interpretation applied — "
               "never answer with words, questions or explanations.")
SYSTEM_CREATE = ("You create advertising creative images. ALWAYS return exactly one image and no text — "
                 "never answer with words, questions or explanations. Photorealistic unless the prompt asks "
                 "for another style; text on the image only when the prompt asks for it, spelled exactly.")


class NanoBananaError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message, self.code = message, code


def configured() -> bool:
    return bool(config.GEMINI_API_KEY)


def price(model: str, size: str) -> float:
    return MODELS.get(model, MODELS[DEFAULT_MODEL])[1].get(size, 0.0)


def sizes_for(model: str) -> list[str]:
    return list(MODELS.get(model, MODELS[DEFAULT_MODEL])[1].keys())


def _extract_image(data: dict) -> tuple[bytes, str]:
    """Last image block across steps[].content[] (also tolerate a top-level
    output_image convenience field if the API ever returns one)."""
    found = None
    for step in data.get("steps", []) or []:
        for block in step.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
                found = block
    if found is None:
        oi = data.get("output_image")
        if isinstance(oi, dict) and oi.get("data"):
            found = oi
    if found is None:
        # the model answered with WORDS instead of a picture — a refusal, or a prompt it read
        # as a question / a non-visual task ("remove the metadata"). Say so, quote it, and
        # tell the operator what to do instead.
        texts = []
        for step in data.get("steps", []) or []:
            for block in step.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        said = " ".join(t.strip() for t in texts).replace("\n", " ")[:220]
        raise NanoBananaError(("The AI replied with text instead of an edited image"
                               + (f": “{said}…”" if said else "")
                               + " — describe a VISIBLE change (background, colours, text on the image, product placement)."
                               " To make a copy TikTok sees as a new file, use Variations (uniquify) — that is not an AI edit."),
                              "no_image")
    return base64.b64decode(found["data"]), found.get("mime_type") or "image/png"


def generate(prompt: str, image: bytes | None = None, image_mime: str = "image/png",
             model: str = DEFAULT_MODEL, size: str = DEFAULT_SIZE,
             aspect: str = "", timeout: float = 120.0) -> tuple[bytes, str]:
    """Edit `image` per `prompt` (or create from text when image is None).
    Returns (png/jpeg bytes, mime)."""
    if not configured():
        raise NanoBananaError("GEMINI_API_KEY is not set", "not_configured")
    if model not in MODELS:
        model = DEFAULT_MODEL
    if size == "0.5K":
        size = "512"                    # rows queued before the enum fix
    if size not in MODELS[model][1]:
        size = DEFAULT_SIZE if DEFAULT_SIZE in MODELS[model][1] else sizes_for(model)[0]
    inputs: list[dict] = [{"type": "text", "text": prompt.strip()}]
    if image:
        inputs.append({"type": "image", "mime_type": image_mime,
                       "data": base64.b64encode(image).decode("ascii")})
    # Google now accepts ONLY image/jpeg here (Sep 2026: "The value 'image/png' is not supported
    # for 'response_format.mime_type'. Supported values: 'image/jpeg'") — the result is stored as .jpg
    fmt: dict = {"type": "image", "mime_type": "image/jpeg", "image_size": size}
    if aspect in ASPECTS:
        fmt["aspect_ratio"] = aspect
    system = SYSTEM_EDIT if image else SYSTEM_CREATE
    body = {"model": model, "input": inputs, "response_format": fmt, "system_instruction": system}
    try:
        return _call(body, timeout)
    except NanoBananaError as e:
        # Two guarded retries, one each:
        #  * the image endpoint rejects the system_instruction field → send without it
        #  * the model still answered with words → fold the "image only" rule into the prompt
        if e.code == "400" and "system_instruction" in e.message:
            body.pop("system_instruction", None)
            return _call(body, timeout)
        if e.code == "no_image":
            body["input"][0] = {"type": "text", "text": f"{system}\n\nInstruction: {prompt.strip()}\n\nReturn only the image."}
            return _call(body, timeout)
        raise


def _call(body: dict, timeout: float) -> tuple[bytes, str]:
    try:
        r = httpx.post(ENDPOINT, json=body, timeout=timeout,
                       headers={"x-goog-api-key": config.GEMINI_API_KEY,
                                "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise NanoBananaError(f"network error: {e}", "network") from e
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:300]
        raise NanoBananaError(f"HTTP {r.status_code}: {msg or r.text[:200]}", str(r.status_code))
    data = r.json()
    if data.get("errors"):
        raise NanoBananaError("; ".join(str(e.get("message", e)) for e in data["errors"])[:300], "api")
    if data.get("status") not in (None, "completed"):
        raise NanoBananaError(f"unexpected status {data.get('status')}", "status")
    return _extract_image(data)
