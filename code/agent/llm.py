"""Minimal Gemini client: no SDK dependency, disk cache, retries, token accounting.

Configuration (environment or repo-root .env):
  GEMINI_API_KEY   required for LLM features (never logged or written anywhere)
  GEMINI_MODEL     default gemini-3.5-flash
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache" / "llm"
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()


class Usage:
    """Per-model counters, written to code/evaluation/usage.json after a run."""
    calls: dict = {}

    @classmethod
    def add(cls, model, inp, out, cached):
        c = cls.calls.setdefault(model, {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0})
        if cached:
            c["cache_hits"] += 1
        else:
            c["calls"] += 1
        c["input_tokens"] += inp
        c["output_tokens"] += out

    @classmethod
    def dump(cls, path: Path):
        path.write_text(json.dumps(cls.calls, indent=2))


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")


def model_chain() -> list[str]:
    """Primary model first, then fallbacks used when a free-tier daily quota is exhausted."""
    fallbacks = os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    chain = [model_name()] + [m.strip() for m in fallbacks.split(",") if m.strip()]
    return list(dict.fromkeys(chain))


class QuotaExhausted(RuntimeError):
    pass


def _cache_path(model: str, prompt: str, data: bytes | None) -> Path:
    h = hashlib.sha256((model + prompt).encode())
    if data is not None:
        h.update(data)
    return CACHE_DIR / f"{h.hexdigest()[:40]}.json"


def generate_json(prompt: str, image_path: Path | None = None, schema: dict | None = None,
                  model: str | None = None, retries: int = 6):
    """Return parsed JSON from Gemini. Cached per (model, prompt, attachment); any model in the
    fallback chain that already answered is reused before a new call is made."""
    chain = [model] if model else model_chain()
    data = Path(image_path).read_bytes() if image_path is not None else None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for m in chain:
        cache = _cache_path(m, prompt, data)
        if cache.exists():
            hit = json.loads(cache.read_text())
            Usage.add(m, hit["usage"]["in"], hit["usage"]["out"], cached=True)
            return hit["result"]
    last = None
    for m in chain:
        try:
            return _call(m, prompt, image_path, data, schema, retries)
        except QuotaExhausted as exc:
            last = exc
            print(f"[llm] daily quota exhausted for {m}; falling back")
    raise last or RuntimeError("no model available")


def _call(model, prompt, image_path, data, schema, retries):
    parts = [{"text": prompt}]
    if data is not None:
        mime = "application/pdf" if str(image_path).endswith(".pdf") else "image/png"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
    cfg = {"temperature": 0, "responseMimeType": "application/json"}
    if schema:
        cfg["responseSchema"] = schema
    body = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}).encode()
    req = urllib.request.Request(API.format(model=model), data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    delay = 10.0
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                d = json.loads(resp.read())
            text = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
            meta = d.get("usageMetadata", {})
            usage = {"in": meta.get("promptTokenCount", 0),
                     "out": meta.get("candidatesTokenCount", 0) + meta.get("thoughtsTokenCount", 0)}
            result = json.loads(text)
            Usage.add(model, usage["in"], usage["out"], cached=False)
            _cache_path(model, prompt, data).write_text(
                json.dumps({"model": model, "usage": usage, "result": result}, ensure_ascii=False))
            return result
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code == 429 and "PerDay" in msg:
                raise QuotaExhausted(model) from None
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {msg[:300]}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise
