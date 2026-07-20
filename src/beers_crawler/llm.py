"""Optional LLM helpers for turning menu/OCR text into Untappd search keywords.

Uses an OpenAI-compatible Chat Completions API (DeepSeek by default).

Env (never commit real keys):
  BEERS_CRAWLER_LLM_API_KEY     — required to enable
  BEERS_CRAWLER_LLM_BASE_URL    — default https://api.deepseek.com
  BEERS_CRAWLER_LLM_MODEL       — default deepseek-chat
  BEERS_CRAWLER_LLM_ENABLED     — 1/true to allow (also implied when API key set)
  BEERS_CRAWLER_LLM_TIMEOUT     — seconds (default 12)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

SYSTEM_PROMPT = """You help map messy beer-menu / OCR text to Untappd search keywords.
Untappd search works best with the BEER NAME only (not "Brewery + Beer" as one string).
The brewery is used afterward to pick the right hit among namesakes.

Rules:
- Expand abbreviations: St./St → Street, Co → Company when part of a brewery.
- Strip serving markers like (s), glass sizes, prices.
- Strip style tags from the beer name when they are not part of the proper name:
  IPA, DIPA, IIPA (=double IPA), 3xIPA/TIPA (=triple IPA), APA, NEIPA, etc.
- Fix obvious OCR typos when confident (Curios→Curious, lIPA→IPA).
- Prefer the base beer name over vanilla/double/year variants unless the menu clearly wants a variant.
- Return STRICT JSON only, no markdown:
{
  "brewery_name": "string or empty",
  "beer_name": "string",
  "search_queries": ["beer name for Algolia", "optional alt", "..."]
}
search_queries: 1-4 short strings, beer-name-first (brewery only if the beer name alone is too generic).
"""


@dataclass(frozen=True)
class LLMKeywordGuess:
    brewery_name: str
    beer_name: str
    search_queries: list[str]


def llm_enabled() -> bool:
    key = (os.environ.get("BEERS_CRAWLER_LLM_API_KEY") or "").strip()
    if not key:
        return False
    flag = os.environ.get("BEERS_CRAWLER_LLM_ENABLED", "1").lower()
    return flag not in {"0", "false", "no"}


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


async def guess_search_keywords(menu_line: str) -> Optional[LLMKeywordGuess]:
    """Ask the LLM for Untappd-oriented beer_name + search_queries."""
    if not llm_enabled():
        return None
    api_key = (os.environ.get("BEERS_CRAWLER_LLM_API_KEY") or "").strip()
    base = (os.environ.get("BEERS_CRAWLER_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip(
        "/"
    )
    model = os.environ.get("BEERS_CRAWLER_LLM_MODEL") or DEFAULT_MODEL
    try:
        timeout = float(os.environ.get("BEERS_CRAWLER_LLM_TIMEOUT") or "12")
    except ValueError:
        timeout = 12.0

    url = f"{base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 256,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Menu line:\n{menu_line.strip()}\n\nJSON:",
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "LLM keyword guess HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None
            body = resp.json()
        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        data = _extract_json_object(content or "")
        if not data:
            logger.warning("LLM keyword guess: no JSON in response")
            return None
        beer = str(data.get("beer_name") or "").strip()
        brewery = str(data.get("brewery_name") or "").strip()
        queries_raw = data.get("search_queries") or []
        queries: list[str] = []
        if isinstance(queries_raw, list):
            for q in queries_raw:
                s = " ".join(str(q).split())
                if s and s.lower() not in {x.lower() for x in queries}:
                    queries.append(s)
        if beer and beer.lower() not in {x.lower() for x in queries}:
            queries.insert(0, beer)
        if not queries:
            return None
        logger.info(
            "LLM keywords for %r → beer=%r brewery=%r queries=%s",
            menu_line,
            beer,
            brewery,
            queries,
        )
        return LLMKeywordGuess(
            brewery_name=brewery,
            beer_name=beer or queries[0],
            search_queries=queries[:4],
        )
    except Exception as exc:
        logger.warning("LLM keyword guess failed: %s", exc)
        return None
