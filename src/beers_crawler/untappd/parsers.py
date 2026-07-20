from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from beers_crawler.models import BeerMetadata, BeerPageRef, utc_now

UNTAPPD_ORIGIN = "https://untappd.com"
BEER_PATH_RE = re.compile(
    r"(?:https?://(?:www\.)?untappd\.com)?/b/([a-z0-9\-]+)/(\d+)",
    re.I,
)

# Real beer search hits live here; featured mega-brands sit in sidebar.
PRIMARY_LIST_SELECTORS = (
    "div.results-container",
    "div.result-list.beer-list",
    "div.result-list",
    "div.beer-list",
)
BEER_ITEM_SELECTOR = "div.beer-item"
SIDEBAR_SELECTOR = "div.sidebar"

# Soft-penalize when not in query (featured / promo junk).
MEGA_BRANDS = frozenset(
    {
        "guinness",
        "corona",
        "heineken",
        "stella",
        "modelo",
        "budweiser",
        "bud-light",
        "michelob",
        "coors",
        "miller",
    }
)

# Tiny words that shouldn't count as beer-name anchors.
STOP_TOKENS = frozenset(
    {
        "the",
        "and",
        "ale",
        "ipa",
        "beer",
        "lager",
        "brew",
        "brewing",
        "brewery",
        "company",
        "co",
        "inc",
        "llc",
        "by",  # "Bombay by Boat" — keep boat/bombay distinctive
        "of",
        "for",
        "with",
    }
)

# Style / form words often appended by OCR/menus but missing from Untappd beer_name.
# "Firestone Walker Wandering Don IPA" → beer is just "Wandering Don" in the index.
STYLE_SUFFIX_TOKENS = frozenset(
    {
        "ipa",
        "dipa",
        "ddh",
        "neipa",
        "hazy",
        "pale",
        "ale",
        "lager",
        "pils",
        "pilsner",
        "pilsener",
        "stout",
        "porter",
        "sour",
        "gose",
        "wheat",
        "wit",
        "witbier",
        "kolsch",
        "kölsch",
        "saison",
        "farmhouse",
        "barleywine",
        "barley",
        "wine",
        "tripel",
        "dubbel",
        "quad",
        "bock",
        "doppelbock",
        "helles",
        "marzen",
        "märzen",
        "oktoberfest",
        "amber",
        "brown",
        "red",
        "blonde",
        "blond",
        "black",
        "white",
        "session",
        "double",
        "triple",
        "imperial",
        "nitro",
        "draft",
        "draught",
        "can",
        "bottle",
        "pint",
        "beer",
    }
)

# Multi-word brewery prefixes (first N content tokens count as brewery).
KNOWN_BREWERY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("firestone", "walker"),
    ("russian", "river"),
    ("sierra", "nevada"),
    ("dogfish", "head"),
    ("new", "belgium"),
    ("new", "glarus"),
    ("bells",),
    ("bell's",),
    ("stone",),
    ("lagunitas",),
    ("deschutes",),
    ("allagash",),
    ("moonlight",),
    ("fieldwork",),
    ("odell",),
    ("odell", "brewing"),
)


def strip_style_suffixes(query: str) -> str:
    """Remove trailing style words that hurt exact-ish search engines.

    Keeps at least two content tokens so we never collapse to a single word.
    """
    raw = [t for t in re.split(r"\s+", query.strip()) if t]
    if len(raw) < 3:
        return query.strip()
    out = list(raw)
    while len(out) >= 3:
        last = re.sub(r"[^a-z0-9]+", "", out[-1].lower())
        if last in STYLE_SUFFIX_TOKENS or last in STOP_TOKENS:
            out.pop()
            continue
        break
    return " ".join(out)


def search_query_variants(query: str) -> list[str]:
    """Ordered unique query strings to try against search backends."""
    q0 = " ".join(query.split())
    if not q0:
        return []
    variants: list[str] = [q0]
    stripped = strip_style_suffixes(q0)
    if stripped and stripped.lower() != q0.lower():
        variants.append(stripped)

    # Drop internal stopwords (of/the/by/and) — OCR often keeps them while
    # Untappd beer_name uses punctuation ("Ol' Ivander" vs "Of Ivander").
    def drop_stops(s: str) -> str:
        parts = [
            t
            for t in re.split(r"\s+", s)
            if re.sub(r"[^a-z0-9]+", "", t.lower()) not in STOP_TOKENS
        ]
        return " ".join(parts)

    for base in list(variants):
        ds = drop_stops(base)
        if ds and ds.lower() != base.lower():
            variants.append(ds)

    # If query looks like "Brewery Beer…", also try beer tokens alone and
    # "Beer Brewery" reorder (helps Algolia when brewery+name order is weak).
    brewery, beer = split_query_hints(q0)
    if brewery and beer:
        # preserve order of beer tokens as they appear in query
        ordered_beer = [
            t
            for t in re.split(r"[^a-z0-9]+", q0.lower())
            if t in beer
        ]
        ordered_brew = [
            t
            for t in re.split(r"[^a-z0-9]+", q0.lower())
            if t in brewery
        ]
        if ordered_beer:
            variants.append(" ".join(ordered_beer))
        if ordered_brew and ordered_beer:
            variants.append(" ".join(ordered_beer + ordered_brew))

    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        v = " ".join(v.split())
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def normalize_beer_url(url: str) -> Optional[str]:
    m = BEER_PATH_RE.search(url)
    if not m:
        return None
    slug, beer_id = m.group(1), m.group(2)
    return f"{UNTAPPD_ORIGIN}/b/{slug}/{beer_id}"


def _slug_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 2}


def _normalized_phrase(text: str) -> str:
    """Lowercase alnum joined with hyphens (mirrors toronado normalizedToken)."""
    parts = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(parts)


def split_query_hints(query: str) -> tuple[set[str], set[str]]:
    """Heuristic split of free-text query into brewery-ish vs beer-name tokens.

    Untappd queries are usually ``\"Brewery Beer Name\"``. Known two-word
    breweries (Firestone Walker, Russian River, …) take priority so beer-name
    tokens stay intact. Otherwise prefer a single leading brewery token for
    short queries (``Moonlight Bombay by Boat``).
    """
    # Score/match should ignore trailing style suffixes
    q = strip_style_suffixes(query)
    raw = [t for t in re.split(r"[^a-z0-9]+", q.lower()) if t]
    content = [t for t in raw if t not in STOP_TOKENS and len(t) >= 2]
    if not content:
        return set(), set(raw)

    if len(content) <= 2:
        return set(), set(content)

    # Known multi-word brewery at start
    for prefix in KNOWN_BREWERY_PREFIXES:
        pref = [p for p in prefix if p not in STOP_TOKENS and len(p) >= 2]
        if not pref:
            continue
        n = len(pref)
        if content[:n] == list(pref) and len(content) > n:
            brewery = set(pref)
            beer = set(content[n:])
            beer = {t for t in beer if t not in STOP_TOKENS} or set(content[n:])
            return brewery, beer

    # ≥5 content tokens: two-word brewery guess
    if len(content) >= 5:
        brewery = set(content[:2])
        beer = set(content[2:])
    else:
        brewery = {content[0]}
        beer = set(content[1:])

    beer = {t for t in beer if t not in STOP_TOKENS} or set(content[1:])
    return brewery, beer


def match_score(query: str, slug: str, link_text: str = "", *, in_primary_list: bool = True) -> float:
    """Score a candidate beer page against a free-text query.

    Higher is better. Incorporates:
    - token overlap on slug + link text (style/stop tokens ignored for overlap)
    - brewery / beer-name token presence (toronado-style)
    - mega-brand penalty
    - primary list vs sidebar/nav placement
    """
    # Ignore style suffixes and stopwords when measuring overlap so
    # "Fieldwork Of Ivander" does not match every Fieldwork "… of …" beer.
    q_raw = _slug_tokens(strip_style_suffixes(query))
    q = {t for t in q_raw if t not in STOP_TOKENS and t not in STYLE_SUFFIX_TOKENS}
    if not q:
        q = q_raw
    hay = f"{slug.replace('-', ' ')} {link_text}"
    s = _slug_tokens(hay)
    slug_l = slug.lower()
    if not q:
        return 0.0

    overlap = len(q & s) / len(q)
    shared = q & s
    bonus = 0.15 if len(shared) >= 2 else 0.0
    if len(shared) >= 3:
        bonus += 0.1

    brewery_tokens, beer_tokens = split_query_hints(query)
    brewery_hit = bool(brewery_tokens and (brewery_tokens & s))
    # Beer-name: prefer distinctive tokens (≥3 chars) appearing in slug
    distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
    beer_hit = False
    if distinctive_beer:
        beer_hit = any(t in slug_l for t in distinctive_beer) or bool(distinctive_beer & s)
    elif beer_tokens:
        beer_hit = bool(beer_tokens & s)

    if brewery_tokens and beer_tokens:
        if brewery_hit and beer_hit:
            bonus += 0.25
        elif beer_hit and not brewery_hit:
            # Beer name alone can still win (toronado allows this)
            bonus += 0.05
        elif brewery_hit and not beer_hit:
            # Brewery-only match without the beer name is almost always wrong
            # (Algolia removeWordsIfNoResults returns other beers from same brewery).
            overlap -= 0.55
        else:
            # Neither brewery nor beer name — almost certainly wrong
            overlap -= 0.5
    elif beer_tokens and not beer_hit:
        overlap -= 0.25

    # Require at least one distinctive beer token when query has any
    if distinctive_beer and not beer_hit:
        return max(0.0, min(0.35, overlap + bonus - 0.2))

    # Phrase-level: beer tokens as hyphenated run in slug
    if distinctive_beer:
        ordered = [
            t
            for t in re.split(r"[^a-z0-9]+", query.lower())
            if t in distinctive_beer
        ]
        beer_phrase = "-".join(ordered)
        if beer_phrase and len(beer_phrase) >= 4 and beer_phrase in slug_l:
            bonus += 0.1

    blob = " ".join(q)
    for brand in MEGA_BRANDS:
        brand_in_candidate = brand in s or brand in slug_l.replace("-", " ")
        if brand_in_candidate and brand not in blob:
            overlap -= 0.8
            break

    if not in_primary_list:
        overlap -= 0.35

    return max(0.0, min(1.0, overlap + bonus))


def _ancestor_classes(tag: Tag, depth: int = 8) -> set[str]:
    classes: set[str] = set()
    cur: Optional[Tag] = tag
    for _ in range(depth):
        if cur is None or not isinstance(cur, Tag):
            break
        for c in cur.get("class") or []:
            classes.add(str(c).lower())
        # also record id-ish signals via name
        cur = cur.parent if isinstance(cur.parent, Tag) else None
    return classes


def _in_sidebar(tag: Tag) -> bool:
    classes = _ancestor_classes(tag)
    return "sidebar" in classes


def _in_primary_list(tag: Tag) -> bool:
    classes = _ancestor_classes(tag)
    if "sidebar" in classes:
        return False
    return bool(
        classes
        & {
            "beer-item",
            "results-container",
            "beer-list",
            "result-list",
        }
    )


def _beer_item_link_text(item: Tag, anchor: Tag) -> str:
    """Prefer full beer-item text (name + brewery) over bare anchor label."""
    name_el = item.select_one("p.name, .name")
    brew_el = item.select_one("p.brewery, .brewery, a[href*='/brewery/'], a[href*='/w/']")
    parts = [
        name_el.get_text(" ", strip=True) if name_el else "",
        brew_el.get_text(" ", strip=True) if brew_el else "",
        anchor.get_text(" ", strip=True),
    ]
    return " ".join(p for p in parts if p)


def parse_search_results(html: str, query: str) -> list[BeerPageRef]:
    """Extract beer page candidates from Untappd search HTML.

    Prefers anchors inside the main beer results list (``div.beer-item`` /
    ``div.results-container``) over sidebar featured brands and bare raw URLs.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    results: list[BeerPageRef] = []

    def add(
        norm: str,
        slug: str,
        beer_id: str,
        text: str,
        *,
        in_primary: bool,
        source: str,
    ) -> None:
        if norm in seen:
            return
        seen.add(norm)
        score = match_score(query, slug, text, in_primary_list=in_primary)
        # Hard filter: if query has both brewery + beer hints, require at least
        # one beer-name token in slug/text when score would otherwise pass.
        brewery_tokens, beer_tokens = split_query_hints(query)
        distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
        if brewery_tokens and distinctive_beer:
            hay = f"{slug} {text}".lower()
            if not any(t in hay for t in distinctive_beer):
                score = min(score, 0.2)
        results.append(
            BeerPageRef(
                query=query,
                page_url=norm,
                slug=slug,
                beer_id=beer_id,
                match_score=score,
                source=source,
            )
        )

    # 1) Primary list beer-items first (best signal)
    primary_roots: list[Tag] = []
    for sel in PRIMARY_LIST_SELECTORS:
        primary_roots.extend(soup.select(sel))
    if not primary_roots:
        primary_roots = [soup]  # type: ignore[list-item]

    items = []
    for root in primary_roots:
        items.extend(root.select(BEER_ITEM_SELECTOR))

    if items:
        for item in items:
            if _in_sidebar(item):
                continue
            for a in item.find_all("a", href=True):
                href = a["href"]
                norm = normalize_beer_url(
                    href if href.startswith("http") else urljoin(UNTAPPD_ORIGIN, href)
                )
                if not norm:
                    continue
                m = BEER_PATH_RE.search(norm)
                if not m:
                    continue
                text = _beer_item_link_text(item, a)
                add(
                    norm,
                    m.group(1),
                    m.group(2),
                    text,
                    in_primary=True,
                    source="untappd_search_list",
                )
    else:
        # Fallback: any anchor not in sidebar
        for a in soup.find_all("a", href=True):
            href = a["href"]
            norm = normalize_beer_url(
                href if href.startswith("http") else urljoin(UNTAPPD_ORIGIN, href)
            )
            if not norm:
                continue
            m = BEER_PATH_RE.search(norm)
            if not m:
                continue
            in_primary = _in_primary_list(a) and not _in_sidebar(a)
            source = "untappd_search_list" if in_primary else "untappd_search"
            add(
                norm,
                m.group(1),
                m.group(2),
                a.get_text(" ", strip=True),
                in_primary=in_primary,
                source=source,
            )

    # 2) Remaining anchors (sidebar etc.) at lower priority
    for a in soup.find_all("a", href=True):
        href = a["href"]
        norm = normalize_beer_url(
            href if href.startswith("http") else urljoin(UNTAPPD_ORIGIN, href)
        )
        if not norm or norm in seen:
            continue
        m = BEER_PATH_RE.search(norm)
        if not m:
            continue
        in_primary = _in_primary_list(a) and not _in_sidebar(a)
        add(
            norm,
            m.group(1),
            m.group(2),
            a.get_text(" ", strip=True),
            in_primary=in_primary,
            source="untappd_search_sidebar" if _in_sidebar(a) else "untappd_search",
        )

    # 3) Raw HTML URLs missed by anchors
    for m in BEER_PATH_RE.finditer(html):
        norm = f"{UNTAPPD_ORIGIN}/b/{m.group(1)}/{m.group(2)}"
        if norm in seen:
            continue
        add(
            norm,
            m.group(1),
            m.group(2),
            "",
            in_primary=False,
            source="untappd_search_raw",
        )

    results.sort(key=lambda r: r.match_score, reverse=True)
    return results


def best_search_result(
    html: str, query: str, min_score: float = 0.4
) -> Optional[BeerPageRef]:
    results = parse_search_results(html, query)
    if not results:
        return None

    # Prefer real search-list hits over sidebar/nav featured brands.
    # Static httpx HTML often has only sidebar links — reject weak ones.
    list_hits = [r for r in results if r.source == "untappd_search_list"]
    pool = list_hits if list_hits else results

    top = pool[0]
    if top.match_score < min_score:
        return None

    # Sidebar-only mega-brand with middling score is almost never right
    if not list_hits and top.source in {"untappd_search", "untappd_search_sidebar", "untappd_search_raw"}:
        slug_l = (top.slug or "").lower()
        if any(b in slug_l for b in MEGA_BRANDS) and top.match_score < 0.6:
            return None

    # When query looks like "Brewery + Beer", require beer token in winner
    brewery_tokens, beer_tokens = split_query_hints(query)
    distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
    if brewery_tokens and distinctive_beer:
        hay = f"{top.slug or ''} {top.page_url}".lower()
        if not any(t in hay for t in distinctive_beer):
            # try next candidates that satisfy beer token
            for cand in pool[1:]:
                if cand.match_score < min_score:
                    break
                hay_c = f"{cand.slug or ''} {cand.page_url}".lower()
                if any(t in hay_c for t in distinctive_beer):
                    return cand
            return None
    return top


def _first_number(patterns: list[str], text: str) -> Optional[float]:
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
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
        brew_el = soup.select_one(
            "p.brewery a, .brewery a, a[href*='/brewery/'], a[href*='/w/']"
        )
        if brew_el:
            brewery = brew_el.get_text(strip=True)

    if style is None:
        style_el = soup.select_one("p.style, .style")
        if style_el:
            style = style_el.get_text(strip=True)

    desc_el = soup.select_one(
        "div.beer-descrption-read-less, div.beer-description, .beer-descrption"
    )
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
                r"ABV[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%",
                r'"abv"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?',
            ],
            text,
        )

    if ibu is None:
        ibu = _first_number(
            [
                r"IBU[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)",
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
