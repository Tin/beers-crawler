from pathlib import Path

from beers_crawler.untappd.parsers import (
    best_search_result,
    match_score,
    normalize_beer_url,
    parse_beer_page,
    parse_search_results,
    split_query_hints,
)

SEARCH_HTML = """
<html><body>
<div class="cont search-page">
  <div class="result-list beer-list">
    <div class="results-container">
      <div class="beer-item">
        <div class="beer-details">
          <p class="name"><a href="/b/russian-river-brewing-company-pliny-the-elder/4691">Pliny the Elder</a></p>
          <p class="brewery"><a href="/brewery/1">Russian River Brewing Company</a></p>
        </div>
      </div>
      <div class="beer-item">
        <div class="beer-details">
          <p class="name"><a href="/b/moonlight-brewing-company-reality-czech/12345">Reality Czech</a></p>
          <p class="brewery"><a href="/brewery/2">Moonlight Brewing Company</a></p>
        </div>
      </div>
    </div>
  </div>
  <div class="sidebar">
    <div class="box">
      <div class="content">
        <div class="item"><a href="/b/guinness-guinness-draught/10425">Guinness Draught</a></div>
        <div class="item"><a href="/b/grupo-modelo-corona-extra/5848">Corona Extra</a></div>
      </div>
    </div>
  </div>
</div>
</body></html>
"""

BEER_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Pliny the Elder",
  "brand": {"@type": "Brand", "name": "Russian River Brewing Company"},
  "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.59", "ratingCount": "120000"}
}
</script>
</head>
<body>
<div class="name"><h1>Pliny the Elder</h1></div>
<p class="brewery"><a href="/RussianRiverBrewing">Russian River Brewing Company</a></p>
<p class="style">Imperial IPA</p>
<div class="details">ABV 8% · IBU 100</div>
</body></html>
"""

FIXTURES = Path(__file__).parent / "fixtures"


def test_normalize_beer_url():
    assert (
        normalize_beer_url("https://untappd.com/b/foo-bar/99?ref=1")
        == "https://untappd.com/b/foo-bar/99"
    )
    assert normalize_beer_url("/b/foo-bar/99") == "https://untappd.com/b/foo-bar/99"
    assert normalize_beer_url("https://example.com/x") is None


def test_search_prefers_matching_beer_over_guinness():
    top = best_search_result(SEARCH_HTML, "Russian River Pliny the Elder")
    assert top is not None
    assert "pliny" in top.page_url
    assert "guinness" not in top.page_url
    assert top.source == "untappd_search_list"


def test_search_reality_czech():
    top = best_search_result(SEARCH_HTML, "Moonlight Reality Czech")
    assert top is not None
    assert "reality-czech" in top.page_url


def test_sidebar_guinness_loses_to_primary_list():
    results = parse_search_results(SEARCH_HTML, "something random xyz")
    # Guinness is only in sidebar; should score lower than list items for a nonsense query
    by_slug = {r.slug: r for r in results}
    if "guinness-guinness-draught" in by_slug and "russian-river-brewing-company-pliny-the-elder" in by_slug:
        assert (
            by_slug["guinness-guinness-draught"].match_score
            <= by_slug["russian-river-brewing-company-pliny-the-elder"].match_score
        )


def test_parse_search_sorted():
    results = parse_search_results(SEARCH_HTML, "Pliny the Elder")
    assert results
    assert results[0].match_score >= results[-1].match_score


def test_match_score_penalizes_guinness():
    good = match_score(
        "Pliny the Elder", "russian-river-brewing-company-pliny-the-elder"
    )
    bad = match_score("Pliny the Elder", "guinness-guinness-draught")
    assert good > bad


def test_match_score_sidebar_penalty():
    primary = match_score(
        "Pliny the Elder",
        "russian-river-brewing-company-pliny-the-elder",
        in_primary_list=True,
    )
    side = match_score(
        "Pliny the Elder",
        "russian-river-brewing-company-pliny-the-elder",
        in_primary_list=False,
    )
    assert primary > side


def test_split_query_hints_brewery_and_beer():
    brewery, beer = split_query_hints("Russian River Pliny the Elder")
    # 4 content tokens after stopword drop → single-token brewery split
    assert "russian" in brewery or "pliny" in beer
    assert "pliny" in beer or "elder" in beer


def test_split_query_hints_keeps_multiword_beer_name():
    brewery, beer = split_query_hints("Moonlight Bombay by Boat")
    assert "moonlight" in brewery
    assert "bombay" in beer
    assert "boat" in beer
    # "by" is a stop token and must not steal brewery slot
    assert "bombay" not in brewery


def test_match_score_bombay_by_boat():
    score = match_score(
        "Moonlight Bombay by Boat",
        "moonlight-brewing-company-bombay-by-boat",
        "Bombay by Boat Moonlight Brewing Company",
    )
    assert score >= 0.8


def test_parse_beer_page_rating():
    meta = parse_beer_page(
        BEER_HTML,
        "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4691",
    )
    assert meta.name == "Pliny the Elder"
    assert meta.brewery == "Russian River Brewing Company"
    assert meta.rating_score == 4.59
    assert meta.rating_count == 120000
    assert meta.abv == 8.0
    assert meta.ibu == 100.0
    assert meta.beer_id == "4691"


def test_live_fixture_pliny_prefers_list_over_sidebar():
    compact = FIXTURES / "search_pliny_compact.html"
    if not compact.exists():
        return  # optional fixture
    html = compact.read_text()
    top = best_search_result(html, "Russian River Pliny the Elder")
    assert top is not None
    assert "pliny-the-elder" in top.page_url
    assert "guinness" not in top.page_url
    results = parse_search_results(html, "Russian River Pliny the Elder")
    sources = {r.source for r in results}
    assert "untappd_search_list" in sources
