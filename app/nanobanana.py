"""Gemini image generation / editing ("Nano Banana") — Interactions API.

Contract (ai.google.dev/gemini-api/docs/image-generation + /api/interactions-api):
  POST https://generativelanguage.googleapis.com/v1beta/interactions
  header  x-goog-api-key: <GEMINI_API_KEY>
  body    {"model": "...",
           "input": [{"type":"text","text": prompt},
                     {"type":"image","mime_type":"image/png","data": <base64>}],   # omit for text→image
           "response_format": {"type":"image","mime_type":"image/png","image_size":"1K"}}
  reply   {"status":"completed", "steps":[{"type":"model_output",
             "content":[{"type":"image","mime_type":"image/png","data":<base64>}, ...]}], "usage":{...}}
The generated image is the LAST image block across steps[].content[].

Pricing (ai.google.dev/gemini-api/docs/pricing, standard tier, per image):
  gemini-3.1-flash-lite-image  $0.0336 (1K only)
  gemini-3.1-flash-image       $0.045 0.5K · $0.067 1K · $0.101 2K · $0.151 4K
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
                                    {"0.5K": 0.045, "1K": 0.067, "2K": 0.101, "4K": 0.151}),
    "gemini-3-pro-image":          ("Nano Banana Pro — highest quality",
                                    {"1K": 0.134, "2K": 0.134, "4K": 0.24}),
}
DEFAULT_MODEL = "gemini-3.1-flash-image"
DEFAULT_SIZE = "1K"
ASPECTS = ["1:1", "9:16", "16:9", "4:5", "3:4", "4:3", "2:3", "3:2", "21:9"]


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
        # surface the model's text (often a safety refusal) so the operator sees WHY
        texts = []
        for step in data.get("steps", []) or []:
            for block in step.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        raise NanoBananaError("No image in the response" + (": " + " ".join(texts)[:300] if texts else ""),
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
    if size not in MODELS[model][1]:
        size = sizes_for(model)[0]
    inputs: list[dict] = [{"type": "text", "text": prompt.strip()}]
    if image:
        inputs.append({"type": "image", "mime_type": image_mime,
                       "data": base64.b64encode(image).decode("ascii")})
    fmt: dict = {"type": "image", "mime_type": "image/png", "image_size": size}
    if aspect in ASPECTS:
        fmt["aspect_ratio"] = aspect
    body = {"model": model, "input": inputs, "response_format": fmt}
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
