from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from beers_crawler.models import BeerMetadata, BeerPageRef, utc_now

UNTAPPD_ORIGIN = "https://untappd.com"
BEER_PATH_RE = re.compile(
    r"(?:https?://(?:www\.)?untappd\.com)?/b/([a-z0-9\-]+)/(\d+)",
    re.I,
)


def normalize_beer_url(url: str) -> Optional[str]:
    m = BEER_PATH_RE.search(url)
    if not m:
        return None
    slug, beer_id = m.group(1), m.group(2)
    return f"{UNTAPPD_ORIGIN}/b/{slug}/{beer_id}"


def _slug_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def match_score(query: str, slug: str, link_text: str = "") -> float:
    q = _slug_tokens(query)
    s = _slug_tokens(slug.replace("-", " ") + " " + link_text)
    if not q:
        return 0.0
    overlap = len(q & s) / len(q)
    # Prefer more specific multi-token hits
    bonus = 0.15 if len(q & s) >= 2 else 0.0
    # Soft-penalize mega brands when not in query
    blob = " ".join(q)
    if "guinness" in s and "guinness" not in blob:
        overlap -= 0.8
    return max(0.0, min(1.0, overlap + bonus))


def parse_search_results(html: str, query: str) -> list[BeerPageRef]:
    """Extract beer page candidates from Untappd search HTML."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[BeerPageRef] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        norm = normalize_beer_url(href if href.startswith("http") else urljoin(UNTAPPD_ORIGIN, href))
        if not norm or norm in seen:
            continue
        m = BEER_PATH_RE.search(norm)
        if not m:
            continue
        seen.add(norm)
        slug, beer_id = m.group(1), m.group(2)
        text = a.get_text(" ", strip=True)
        score = match_score(query, slug, text)
        results.append(
            BeerPageRef(
                query=query,
                page_url=norm,
                slug=slug,
                beer_id=beer_id,
                match_score=score,
                source="untappd_search",
            )
        )

    # Also scan raw HTML for absolute URLs missed by anchors
    for m in BEER_PATH_RE.finditer(html):
        norm = f"{UNTAPPD_ORIGIN}/b/{m.group(1)}/{m.group(2)}"
        if norm in seen:
            continue
        seen.add(norm)
        score = match_score(query, m.group(1), "")
        results.append(
            BeerPageRef(
                query=query,
                page_url=norm,
                slug=m.group(1),
                beer_id=m.group(2),
                match_score=score,
                source="untappd_search_raw",
            )
        )

    results.sort(key=lambda r: r.match_score, reverse=True)
    return results


def best_search_result(html: str, query: str, min_score: float = 0.25) -> Optional[BeerPageRef]:
    results = parse_search_results(html, query)
    if not results:
        return None
    top = results[0]
    if top.match_score < min_score:
        return None
    return top


def _first_number(patterns: list[str], text: str) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


def _json_ld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def parse_beer_page(html: str, page_url: str) -> BeerMetadata:
    """Parse Untappd beer detail HTML into metadata (rating_score is primary)."""
    soup = BeautifulSoup(html, "lxml")
    name: Optional[str] = None
    brewery: Optional[str] = None
    style: Optional[str] = None
    description: Optional[str] = None
    rating_score: Optional[float] = None
    rating_count: Optional[int] = None
    abv: Optional[float] = None
    ibu: Optional[float] = None
    beer_id: Optional[str] = None

    m = BEER_PATH_RE.search(page_url)
    if m:
        beer_id = m.group(2)

    for obj in _json_ld_objects(soup):
        if obj.get("@type") in ("Product", "Beer", "Thing") or "name" in obj:
            name = name or (obj.get("name") if isinstance(obj.get("name"), str) else None)
            brand = obj.get("brand")
            if isinstance(brand, dict):
                brewery = brewery or brand.get("name")
            elif isinstance(brand, str):
                brewery = brewery or brand
            agg = obj.get("aggregateRating")
            if isinstance(agg, dict):
                if rating_score is None and agg.get("ratingValue") is not None:
                    try:
                        rating_score = float(agg["ratingValue"])
                    except (TypeError, ValueError):
                        pass
                if rating_count is None and agg.get("ratingCount") is not None:
                    try:
                        rating_count = int(agg["ratingCount"])
                    except (TypeError, ValueError):
                        pass

    # Common Untappd DOM hooks (may change; keep multiple fallbacks)
    if name is None:
        h1 = soup.select_one("div.name h1, .beer-page h1, h1")
        if h1:
            name = h1.get_text(strip=True)

    if brewery is None:
        brew_el = soup.select_one("p.brewery a, .brewery a, a[href*='/brewery/'], a[href*='/w/']")
        if brew_el:
            brewery = brew_el.get_text(strip=True)

    if style is None:
        style_el = soup.select_one("p.style, .style")
        if style_el:
            style = style_el.get_text(strip=True)

    desc_el = soup.select_one("div.beer-descrption-read-less, div.beer-description, .beer-descrption")
    if desc_el:
        description = desc_el.get_text(" ", strip=True) or None

    text = html
    if rating_score is None:
        rating_score = _first_number(
            [
                r'"ratingValue"\s*:\s*"?([0-5](?:\.\d+)?)"?',
                r'"rating_score"\s*:\s*([0-5](?:\.\d+)?)',
                r'class="num"[^>]*>\s*\(?([0-5](?:\.\d+)?)\)?',
                r'data-rating=["\']([0-5](?:\.\d+)?)["\']',
            ],
            text,
        )

    if rating_count is None:
        rc = _first_number(
            [
                r'"ratingCount"\s*:\s*"?(\d+)"?',
                r'"raters_count"\s*:\s*(\d+)',
                r'([\d,]+)\s+Ratings',
            ],
            text,
        )
        if rc is not None:
            rating_count = int(rc)

    if abv is None:
        abv = _first_number(
            [
                r'ABV[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%',
                r'"abv"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
            ],
            text,
        )

    if ibu is None:
        ibu = _first_number(
            [
                r'IBU[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)',
                r'"ibu"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
            ],
            text,
        )

    # Normalize absolute page URL
    path = urlparse(page_url).path
    canonical = normalize_beer_url(page_url) or f"{UNTAPPD_ORIGIN}{path}"

    return BeerMetadata(
        page_url=canonical,
        name=name,
        brewery=brewery,
        style=style,
        abv=abv,
        ibu=ibu,
        rating_score=rating_score,
        rating_count=rating_count,
        description=description,
        beer_id=beer_id,
        scraped_at=utc_now(),
    )
