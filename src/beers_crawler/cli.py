from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from beers_crawler import __version__
from beers_crawler.db import BeerDatabase, default_db_path
from beers_crawler.service import CrawlerService
from beers_crawler.untappd.client import UntappdClient

console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _db_option(f):
    return click.option(
        "--db",
        "db_path",
        type=click.Path(path_type=Path),
        default=None,
        help=f"SQLite path (default: {default_db_path()})",
    )(f)


def _history_options(f):
    f = click.option(
        "--history-only",
        is_flag=True,
        help="Skip live crawl; read latest SQLite history only",
    )(f)
    f = click.option(
        "--no-history",
        is_flag=True,
        help="Do not read or write crawl history",
    )(f)
    return f


@click.group()
@click.version_option(__version__, prog_name="beers-crawler")
def main() -> None:
    """Untappd beer crawler CLI (SQLite history-backed).

    Default policy: try live crawl first; on success append a timestamped
    history row; on failure fall back to the latest history snapshot.

    Interfaces:
      resolve     beer name  → Untappd page URL
      metadata    page URL   → beer metadata (rating score)
      crawl       name → URL → metadata (combined)
      history     page URL   → past crawl snapshots
    """


@main.command("resolve")
@click.argument("beer_name")
@_db_option
@_history_options
@click.option("--headed", is_flag=True, help="Show browser window")
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True, help="Print JSON")
def resolve_cmd(
    beer_name: str,
    db_path: Optional[Path],
    history_only: bool,
    no_history: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Interface 1: beer name → Untappd page URL (live first, history fallback)."""
    _setup_logging(verbose)
    code = asyncio.run(
        _resolve(
            beer_name,
            db_path,
            history_only=history_only,
            use_history=not no_history,
            headless=not headed,
            as_json=as_json,
        )
    )
    sys.exit(code)


async def _resolve(
    beer_name: str,
    db_path: Optional[Path],
    *,
    history_only: bool,
    use_history: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=use_history)
    async with client:
        ref = await service.beer_name_to_url(
            beer_name, history_only=history_only, use_history=use_history
        )
    if ref is None:
        console.print(f"[red]No Untappd page found for[/red] {beer_name!r}")
        return 1
    if as_json:
        console.print_json(ref.model_dump_json())
    else:
        origin = "history" if ref.from_history else "live"
        console.print(f"[bold]query[/bold]   {ref.query}")
        console.print(f"[bold]url[/bold]     {ref.page_url}")
        console.print(f"[bold]match[/bold]   {ref.match_score:.2f}")
        console.print(f"[bold]source[/bold]  {ref.source}  ({origin})")
    return 0


@main.command("metadata")
@click.argument("page_url")
@_db_option
@_history_options
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def metadata_cmd(
    page_url: str,
    db_path: Optional[Path],
    history_only: bool,
    no_history: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Interface 2: Untappd page URL → beer metadata (live first, history fallback)."""
    _setup_logging(verbose)
    code = asyncio.run(
        _metadata(
            page_url,
            db_path,
            history_only=history_only,
            use_history=not no_history,
            headless=not headed,
            as_json=as_json,
        )
    )
    sys.exit(code)


async def _metadata(
    page_url: str,
    db_path: Optional[Path],
    *,
    history_only: bool,
    use_history: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=use_history)
    async with client:
        meta = await service.url_to_metadata(
            page_url, history_only=history_only, use_history=use_history
        )
    if meta is None:
        console.print(f"[red]Failed to fetch metadata for[/red] {page_url}")
        return 1
    if as_json:
        console.print_json(meta.model_dump_json())
    else:
        origin = "history" if meta.from_history else "live"
        console.print(f"[bold]name[/bold]      {meta.name}")
        console.print(f"[bold]brewery[/bold]   {meta.brewery}")
        console.print(f"[bold]style[/bold]     {meta.style}")
        console.print(
            f"[bold]rating[/bold]    {meta.rating_score}  ({meta.rating_count} ratings)"
        )
        console.print(f"[bold]abv[/bold]       {meta.abv}")
        console.print(f"[bold]ibu[/bold]       {meta.ibu}")
        console.print(f"[bold]url[/bold]       {meta.page_url}")
        console.print(f"[bold]scraped[/bold]   {meta.scraped_at.isoformat()}  ({origin})")
        if meta.history_id is not None:
            console.print(f"[bold]history#[/bold]  {meta.history_id}")
    return 0


@main.command("crawl")
@click.argument("beer_name")
@_db_option
@_history_options
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def crawl_cmd(
    beer_name: str,
    db_path: Optional[Path],
    history_only: bool,
    no_history: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Combined: beer name → URL → metadata; append snapshot to history."""
    _setup_logging(verbose)
    code = asyncio.run(
        _crawl(
            beer_name,
            db_path,
            history_only=history_only,
            use_history=not no_history,
            headless=not headed,
            as_json=as_json,
        )
    )
    sys.exit(code)


async def _crawl(
    beer_name: str,
    db_path: Optional[Path],
    *,
    history_only: bool,
    use_history: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=use_history)
    async with client:
        ref, meta = await service.crawl_beer(
            beer_name, history_only=history_only, use_history=use_history
        )
    if ref is None:
        console.print(f"[red]No Untappd page found for[/red] {beer_name!r}")
        return 1
    if as_json:
        payload = {
            "page": ref.model_dump(mode="json"),
            "metadata": meta.model_dump(mode="json") if meta else None,
        }
        console.print_json(json.dumps(payload))
    else:
        origin_ref = "history" if ref.from_history else "live"
        console.print(
            f"[bold green]URL[/bold green]     {ref.page_url}  "
            f"(match {ref.match_score:.2f}, {origin_ref})"
        )
        if meta:
            origin_meta = "history" if meta.from_history else "live"
            console.print(
                f"[bold green]Rating[/bold green]  {meta.rating_score}  ·  "
                f"{meta.name} · {meta.brewery}  ({origin_meta} @ {meta.scraped_at.isoformat()})"
            )
        else:
            console.print("[yellow]Metadata fetch failed (no history either)[/yellow]")
            return 2
    return 0


@main.command("batch")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@_db_option
@click.option(
    "--history-only",
    is_flag=True,
    help="Skip live crawl; read history only",
)
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--delay", default=1.5, show_default=True, help="Seconds between beers")
def batch_cmd(
    file: Path,
    db_path: Optional[Path],
    history_only: bool,
    headed: bool,
    verbose: bool,
    delay: float,
) -> None:
    """Crawl one beer name per line from FILE (each success appends history)."""
    _setup_logging(verbose)
    names = [
        ln.strip()
        for ln in file.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    code = asyncio.run(
        _batch(
            names,
            db_path,
            history_only=history_only,
            headless=not headed,
            delay=delay,
        )
    )
    sys.exit(code)


async def _batch(
    names: list[str],
    db_path: Optional[Path],
    *,
    history_only: bool,
    headless: bool,
    delay: float,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=True)
    ok = 0
    async with client:
        for i, name in enumerate(names, 1):
            console.print(f"[cyan]({i}/{len(names)})[/cyan] {name}")
            ref, meta = await service.crawl_beer(name, history_only=history_only)
            if ref and meta and meta.rating_score is not None:
                tag = "hist" if meta.from_history else "live"
                console.print(
                    f"  → {meta.rating_score}  {ref.page_url}  [{tag}]"
                )
                ok += 1
            elif ref:
                console.print(f"  → URL only {ref.page_url}")
            else:
                console.print("  → [red]failed[/red]")
            if i < len(names) and not history_only:
                await asyncio.sleep(delay)
    console.print(f"Done: {ok}/{len(names)} with scores")
    return 0 if ok else 1


@main.command("list")
@_db_option
@click.option("--limit", default=50, show_default=True)
def list_cmd(db_path: Optional[Path], limit: int) -> None:
    """List latest history snapshot per beer."""
    db = BeerDatabase(db_path)
    rows = db.list_metadata(limit=limit)
    table = Table(title=f"Latest beer snapshots ({db.path})")
    table.add_column("Rating", justify="right")
    table.add_column("Scraped")
    table.add_column("Name")
    table.add_column("Brewery")
    table.add_column("URL")
    for m in rows:
        table.add_row(
            f"{m.rating_score:.2f}" if m.rating_score is not None else "—",
            m.scraped_at.strftime("%Y-%m-%d %H:%M") if m.scraped_at else "—",
            m.name or "—",
            m.brewery or "—",
            m.page_url,
        )
    console.print(table)
    console.print(db.stats())


@main.command("history")
@click.argument("page_url")
@_db_option
@click.option("--limit", default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def history_cmd(page_url: str, db_path: Optional[Path], limit: int, as_json: bool) -> None:
    """Show timestamped crawl history for one Untappd beer page URL."""
    db = BeerDatabase(db_path)
    rows = db.list_metadata_history(page_url, limit=limit)
    if not rows:
        console.print(f"[yellow]No history for[/yellow] {page_url}")
        sys.exit(1)
    if as_json:
        console.print_json(json.dumps([r.model_dump(mode="json") for r in rows]))
        return
    table = Table(title=f"History · {page_url}")
    table.add_column("ID", justify="right")
    table.add_column("Scraped")
    table.add_column("Rating", justify="right")
    table.add_column("Count", justify="right")
    table.add_column("Name")
    for m in rows:
        table.add_row(
            str(m.history_id or "—"),
            m.scraped_at.isoformat() if m.scraped_at else "—",
            f"{m.rating_score:.3f}" if m.rating_score is not None else "—",
            str(m.rating_count) if m.rating_count is not None else "—",
            m.name or "—",
        )
    console.print(table)


@main.command("init-db")
@_db_option
def init_db_cmd(db_path: Optional[Path]) -> None:
    """Create SQLite schema."""
    db = BeerDatabase(db_path)
    console.print(f"Initialized {db.path}")
    console.print(db.stats())


@main.command("stats")
@_db_option
@click.option("--json", "as_json", is_flag=True)
def stats_cmd(db_path: Optional[Path], as_json: bool) -> None:
    """Show SQLite history counts."""
    db = BeerDatabase(db_path)
    s = db.stats()
    if as_json:
        console.print_json(json.dumps(s))
    else:
        console.print(f"[bold]db[/bold]  {db.path}")
        for k, v in s.items():
            console.print(f"  {k}: {v}")


@main.command("candidates")
@click.argument("beer_name")
@_db_option
@_history_options
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--limit", default=10, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def candidates_cmd(
    beer_name: str,
    db_path: Optional[Path],
    history_only: bool,
    no_history: bool,
    headed: bool,
    verbose: bool,
    limit: int,
    as_json: bool,
) -> None:
    """List ranked Untappd search candidates for a beer name."""
    _setup_logging(verbose)
    code = asyncio.run(
        _candidates(
            beer_name,
            db_path,
            history_only=history_only,
            use_history=not no_history,
            headless=not headed,
            limit=limit,
            as_json=as_json,
        )
    )
    sys.exit(code)


async def _candidates(
    beer_name: str,
    db_path: Optional[Path],
    *,
    history_only: bool,
    use_history: bool,
    headless: bool,
    limit: int,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_history=use_history)
    async with client:
        refs = await service.beer_name_to_candidates(
            beer_name,
            history_only=history_only,
            use_history=use_history,
            limit=limit,
        )
    if not refs:
        console.print(f"[red]No candidates for[/red] {beer_name!r}")
        return 1
    if as_json:
        console.print_json(json.dumps([r.model_dump(mode="json") for r in refs]))
    else:
        table = Table(title=f"Candidates for {beer_name!r}")
        table.add_column("#", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Source")
        table.add_column("URL")
        for i, r in enumerate(refs, 1):
            table.add_row(str(i), f"{r.match_score:.2f}", r.source, r.page_url)
        console.print(table)
    return 0


@main.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8741, show_default=True, type=int)
@click.option("--reload", is_flag=True, help="Dev auto-reload")
@click.option("--headed", is_flag=True, help="Show browser (sets BEERS_CRAWLER_HEADED)")
@_db_option
def serve_cmd(
    host: str, port: int, reload: bool, headed: bool, db_path: Optional[Path]
) -> None:
    """Run FastAPI HTTP server (resolve / metadata / crawl / history)."""
    import os

    import uvicorn

    if db_path is not None:
        os.environ["BEERS_CRAWLER_DB"] = str(db_path)
    if headed:
        os.environ["BEERS_CRAWLER_HEADED"] = "1"
    console.print(f"Serving on http://{host}:{port}  (docs /docs)")
    uvicorn.run(
        "beers_crawler.api:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
