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
        "iipa",  # double IPA acronym (also normalized from IIPA)
        "tipa",  # triple IPA
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
    ("alvarado", "street"),
    ("alvarado", "st"),
    ("half", "acre"),
    ("urban", "roots"),
    ("great", "notion"),
    ("barreled", "souls"),
    ("pure", "project"),
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
    ("cellarmaker",),
    ("monkish",),
    ("highland", "park"),
    ("altamont",),
    ("cooperage",),
)

# Brewery street/abbreviation expansions applied before tokenization.
# Order matters for multi-word replacements.
_BREWERY_ABBREV_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bAlvarado\s+St\.?\b", re.I), "Alvarado Street"),
    (re.compile(r"\bHalf\s+Acre\b", re.I), "Half Acre"),
    (re.compile(r"\bSt\.?\s+Brew", re.I), "Street Brew"),  # generic "… St Brewery"
)

# Menu/OCR IPA strength acronyms → words we strip as styles (or keep as double/triple).
_IPA_ACRONYM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 3xIPA / 3X IPA / xxxIPA → triple ipa (then stripped as style suffix)
    (re.compile(r"\b(\d)\s*[xX×]\s*IPA\b", re.I), r"\1x ipa"),
    (re.compile(r"\bIII\s*IPA\b", re.I), "triple ipa"),
    (re.compile(r"\bII\s*IPA\b", re.I), "double ipa"),
    (re.compile(r"\bIIPA\b", re.I), "double ipa"),
    (re.compile(r"\bDIPA\b", re.I), "double ipa"),
    (re.compile(r"\bTIPA\b", re.I), "triple ipa"),
    (re.compile(r"\b3X?IPA\b", re.I), "triple ipa"),
    (re.compile(r"\b2X?IPA\b", re.I), "double ipa"),
    (re.compile(r"\bx{3}\s*IPA\b", re.I), "triple ipa"),
    (re.compile(r"\bx{2}\s*IPA\b", re.I), "double ipa"),
)


def normalize_menu_query(query: str) -> str:
    """Clean OCR/menu noise before split/search.

    - Expand brewery abbreviations (St. → Street)
    - Normalize IPA strength acronyms (IIPA, 3xIPA)
    - Drop trailing serving markers like (s), (S)
    """
    q = " ".join(query.split())
    if not q:
        return q
    # Serving / size markers often glued by OCR: Haole Punch(s), Evangeline(S)
    q = re.sub(r"\(\s*[sS]\s*\)", " ", q)
    q = re.sub(r"\(\s*\)", " ", q)
    for pat, repl in _BREWERY_ABBREV_PATTERNS:
        q = pat.sub(repl, q)
    # "St." → "Street" can leave a stray period: "Street. Haole"
    q = re.sub(r"\bStreet\.\b", "Street", q)
    q = re.sub(r"\s+\.", " ", q)
    for pat, repl in _IPA_ACRONYM_PATTERNS:
        q = pat.sub(repl, q)
    # "3x ipa" / "double ipa" as trailing styles — collapse to tokens strip_style knows
    q = re.sub(r"\b(\d)\s*x\s+ipa\b", r"\1xipa", q, flags=re.I)
    q = re.sub(r"\bdouble\s+ipa\b", "dipa", q, flags=re.I)
    q = re.sub(r"\btriple\s+ipa\b", "tipa", q, flags=re.I)
    # OCR digit/letter confusion on IPA prefix: lIPA → IPA
    q = re.sub(r"\blIPA\b", "IPA", q)
    return " ".join(q.split())


def strip_style_suffixes(query: str) -> str:
    """Remove trailing style words that hurt exact-ish search engines.

    Keeps at least two content tokens so we never collapse to a single word.
    """
    q = normalize_menu_query(query)
    raw = [t for t in re.split(r"\s+", q.strip()) if t]
    if len(raw) < 2:
        return q.strip()
    out = list(raw)
    # Allow stripping down to 2 tokens (brewery + beer) or 1 beer-only name
    min_keep = 1 if len(out) <= 2 else 2
    while len(out) > min_keep:
        last = re.sub(r"[^a-z0-9]+", "", out[-1].lower())
        # 2xipa / 3xipa / tipa / dipa / iipa
        if re.fullmatch(r"\d+x?ipa", last) or last in {"tipa", "dipa", "iipa"}:
            out.pop()
            continue
        if last in STYLE_SUFFIX_TOKENS or last in STOP_TOKENS:
            out.pop()
            continue
        break
    return " ".join(out)


def _drop_stop_tokens_keep_order(text: str) -> str:
    parts = [
        t
        for t in re.split(r"\s+", text.strip())
        if re.sub(r"[^a-z0-9]+", "", t.lower()) not in STOP_TOKENS
    ]
    return " ".join(parts)


def beer_name_search_string(query: str) -> Optional[str]:
    """Beer-name portion of a free-text query for Untappd/Algolia search.

    Untappd's own UX works best searching the **beer name**, then picking the hit
    whose brewery matches — not ``\"Brewery Beer Name\"`` as one string.
    """
    q0 = strip_style_suffixes(normalize_menu_query(query))
    if not q0:
        return None
    brewery, beer = split_query_hints(q0)
    if beer:
        ordered = [t for t in re.split(r"[^a-z0-9]+", q0.lower()) if t in beer]
        if ordered:
            return " ".join(ordered)
    cleaned = _drop_stop_tokens_keep_order(q0)
    return cleaned or q0


def search_query_variants(query: str) -> list[str]:
    """Ordered unique Algolia/query strings — **beer name first**, then fallbacks.

    Order matters (Untappd-app style):
      1. beer name only (e.g. ``Wandering Don``, ``Haole Punch``)
      2. full query stripped of trailing styles / expanded abbreviations
      3. original query (last resort)
    Brewery is applied later when ranking hits, not in the primary search string.
    """
    original = " ".join(query.split())
    if not original:
        return []
    normalized = normalize_menu_query(original)

    variants: list[str] = []
    beer_only = beer_name_search_string(normalized)
    if beer_only:
        variants.append(beer_only)

    stripped = strip_style_suffixes(normalized)
    if stripped:
        variants.append(stripped)
        ds = _drop_stop_tokens_keep_order(stripped)
        if ds:
            variants.append(ds)

    variants.append(normalized)
    if original.lower() != normalized.lower():
        variants.append(original)

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
    breweries (Firestone Walker, Russian River, Alvarado Street, …) take
    priority so beer-name tokens stay intact.
    """
    q = strip_style_suffixes(normalize_menu_query(query))
    raw = [t for t in re.split(r"[^a-z0-9]+", q.lower()) if t]
    # Keep street/st in the stream for brewery prefix matching; drop from beer side later.
    brewery_noise = {"st", "street", "co", "brewing", "brewery", "company"}
    content = [t for t in raw if t not in STOP_TOKENS and len(t) >= 2]
    if not content:
        return set(), set(raw)

    def _beer_tokens(seq: list[str]) -> set[str]:
        return {t for t in seq if t not in STOP_TOKENS and t not in brewery_noise and len(t) >= 2}

    # Known brewery prefix at start (1+ tokens) — even for short "Brewery Beer" pairs
    for prefix in KNOWN_BREWERY_PREFIXES:
        pref = [p for p in prefix if p not in STOP_TOKENS and len(p) >= 2]
        if not pref:
            continue
        n = len(pref)
        if content[:n] == list(pref) and len(content) > n:
            brewery = set(pref)
            # Expand st → street for matching brewery_name "Alvarado Street …"
            if "st" in brewery:
                brewery.add("street")
            beer = _beer_tokens(content[n:]) or set(content[n:])
            return brewery, beer

    # Single content token — beer only
    if len(content) == 1:
        return set(), set(content)

    # Two tokens, unknown brewery: treat as beer-only (avoid stealing name tokens)
    if len(content) == 2:
        return set(), set(content)

    # ≥5 content tokens: two-word brewery guess
    if len(content) >= 5:
        brewery = set(content[:2])
        beer = _beer_tokens(content[2:]) or set(content[2:])
    else:
        brewery = {content[0]}
        beer = _beer_tokens(content[1:]) or set(content[1:])

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
    # Beer-name: require ALL distinctive tokens (≥3 chars) in slug or text
    distinctive_beer = {t for t in beer_tokens if len(t) >= 3}
    beer_hit = False
    beer_all_hit = False
    if distinctive_beer:
        beer_all_hit = distinctive_beer.issubset(s) or all(
            t in slug_l for t in distinctive_beer
        )
        beer_hit = beer_all_hit or bool(distinctive_beer & s)
    elif beer_tokens:
        beer_hit = bool(beer_tokens & s)
        beer_all_hit = beer_tokens.issubset(s)

    if brewery_tokens and beer_tokens:
        if brewery_hit and beer_all_hit:
            bonus += 0.25
        elif brewery_hit and beer_hit and not beer_all_hit:
            # Partial beer-name match (e.g. only "punch") — weak
            bonus -= 0.15
        elif beer_all_hit and not brewery_hit:
            bonus += 0.05
        elif brewery_hit and not beer_hit:
            overlap -= 0.55
        else:
            overlap -= 0.5
    elif beer_tokens and not beer_hit:
        overlap -= 0.25

    # Missing any distinctive beer token → hard reject floor
    if distinctive_beer and not beer_all_hit:
        return max(0.0, min(0.35, overlap + bonus - 0.25))

    # Phrase-level: beer tokens as hyphenated run in slug
    if distinctive_beer:
        ordered = [
            t
            for t in re.split(
                r"[^a-z0-9]+",
                strip_style_suffixes(normalize_menu_query(query)).lower(),
            )
            if t in distinctive_beer
        ]
        beer_phrase = "-".join(ordered)
        if beer_phrase and len(beer_phrase) >= 4 and beer_phrase in slug_l:
            bonus += 0.1

    # Prefer exact beer_name over longer variants (Haole Punch vs Haole Punch Vanilla)
    exact_title = False
    longer_variant = False
    if distinctive_beer and link_text:
        beer_title = link_text.strip().split("\n")[0].strip()
        title_tokens = {
            t
            for t in re.split(r"[^a-z0-9]+", beer_title.lower())
            if len(t) >= 2
            and t not in STOP_TOKENS
            and t not in STYLE_SUFFIX_TOKENS
        }
        title_beer = title_tokens - brewery_tokens - {
            "brewing",
            "brewery",
            "company",
            "co",
            "street",
            "st",
        }
        if title_beer:
            if title_beer == distinctive_beer:
                exact_title = True
                bonus += 0.2
            elif distinctive_beer < title_beer:
                longer_variant = True
                extra = title_beer - distinctive_beer
                year_only = bool(extra) and all(t.isdigit() for t in extra)
                variant_noise = {
                    "vanilla",
                    "mega",
                    "gnar",
                    "double",
                    "triple",
                    "imperial",
                    "nitro",
                    "howzit",
                }
                if year_only:
                    # Vintage year on an otherwise exact name — mild preference loss only
                    bonus -= 0.05
                    longer_variant = False
                    exact_title = True  # treat as base beer with year
                elif extra & variant_noise:
                    bonus -= 0.35
                else:
                    bonus -= 0.2

    blob = " ".join(q)
    for brand in MEGA_BRANDS:
        brand_in_candidate = brand in s or brand in slug_l.replace("-", " ")
        if brand_in_candidate and brand not in blob:
            overlap -= 0.8
            break

    if not in_primary_list:
        overlap -= 0.35

    score = overlap + bonus
    # Keep flavored/longer variants below exact (or year-stamped) titles
    if longer_variant and not exact_title:
        score = min(score, 0.87)
    return max(0.0, min(1.0, score))


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
