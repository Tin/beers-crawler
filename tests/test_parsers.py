from beers_crawler.untappd.parsers import (
    best_search_result,
    match_score,
    normalize_beer_url,
    parse_beer_page,
    parse_search_results,
)

SEARCH_HTML = """
<html><body>
<a href="/b/guinness-guinness-draught/10425">Guinness Draught</a>
<a href="https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4691">Pliny the Elder</a>
<a href="/b/moonlight-brewing-company-reality-czech/12345">Reality Czech</a>
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


def test_search_reality_czech():
    top = best_search_result(SEARCH_HTML, "Moonlight Reality Czech")
    assert top is not None
    assert "reality-czech" in top.page_url


def test_parse_search_sorted():
    results = parse_search_results(SEARCH_HTML, "Pliny the Elder")
    assert results
    assert results[0].match_score >= results[-1].match_score


def test_match_score_penalizes_guinness():
    good = match_score("Pliny the Elder", "russian-river-brewing-company-pliny-the-elder")
    bad = match_score("Pliny the Elder", "guinness-guinness-draught")
    assert good > bad


def test_parse_beer_page_rating():
    meta = parse_beer_page(BEER_HTML, "https://untappd.com/b/russian-river-brewing-company-pliny-the-elder/4691")
    assert meta.name == "Pliny the Elder"
    assert meta.brewery == "Russian River Brewing Company"
    assert meta.rating_score == 4.59
    assert meta.rating_count == 120000
    assert meta.abv == 8.0
    assert meta.ibu == 100.0
    assert meta.beer_id == "4691"
