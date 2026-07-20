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


@click.group()
@click.version_option(__version__, prog_name="beers-crawler")
def main() -> None:
    """Untappd beer crawler CLI (SQLite-backed).

    Interfaces:
      resolve   beer name  → Untappd page URL
      metadata  page URL   → beer metadata (rating score)
      crawl     name → URL → metadata (combined)
    """


@main.command("resolve")
@click.argument("beer_name")
@_db_option
@click.option("--force", is_flag=True, help="Ignore SQLite cache")
@click.option("--no-cache", is_flag=True, help="Do not read/write cache")
@click.option("--headed", is_flag=True, help="Show browser window")
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True, help="Print JSON")
def resolve_cmd(
    beer_name: str,
    db_path: Optional[Path],
    force: bool,
    no_cache: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Interface 1: beer name → Untappd page URL."""
    _setup_logging(verbose)
    code = asyncio.run(
        _resolve(beer_name, db_path, force=force, use_cache=not no_cache, headless=not headed, as_json=as_json)
    )
    sys.exit(code)


async def _resolve(
    beer_name: str,
    db_path: Optional[Path],
    *,
    force: bool,
    use_cache: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_cache=use_cache)
    async with client:
        ref = await service.beer_name_to_url(beer_name, force=force)
    if ref is None:
        console.print(f"[red]No Untappd page found for[/red] {beer_name!r}")
        return 1
    if as_json:
        console.print_json(ref.model_dump_json())
    else:
        console.print(f"[bold]query[/bold]  {ref.query}")
        console.print(f"[bold]url[/bold]    {ref.page_url}")
        console.print(f"[bold]score[/bold]  {ref.match_score:.2f}")
        console.print(f"[bold]source[/bold] {ref.source}")
    return 0


@main.command("metadata")
@click.argument("page_url")
@_db_option
@click.option("--force", is_flag=True, help="Ignore SQLite cache")
@click.option("--no-cache", is_flag=True)
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def metadata_cmd(
    page_url: str,
    db_path: Optional[Path],
    force: bool,
    no_cache: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Interface 2: Untappd page URL → beer metadata (rating score)."""
    _setup_logging(verbose)
    code = asyncio.run(
        _metadata(page_url, db_path, force=force, use_cache=not no_cache, headless=not headed, as_json=as_json)
    )
    sys.exit(code)


async def _metadata(
    page_url: str,
    db_path: Optional[Path],
    *,
    force: bool,
    use_cache: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_cache=use_cache)
    async with client:
        meta = await service.url_to_metadata(page_url, force=force)
    if meta is None:
        console.print(f"[red]Failed to fetch metadata for[/red] {page_url}")
        return 1
    if as_json:
        console.print_json(meta.model_dump_json())
    else:
        console.print(f"[bold]name[/bold]     {meta.name}")
        console.print(f"[bold]brewery[/bold]  {meta.brewery}")
        console.print(f"[bold]style[/bold]    {meta.style}")
        console.print(f"[bold]rating[/bold]   {meta.rating_score}  ({meta.rating_count} ratings)")
        console.print(f"[bold]abv[/bold]      {meta.abv}")
        console.print(f"[bold]ibu[/bold]      {meta.ibu}")
        console.print(f"[bold]url[/bold]      {meta.page_url}")
    return 0


@main.command("crawl")
@click.argument("beer_name")
@_db_option
@click.option("--force", is_flag=True)
@click.option("--no-cache", is_flag=True)
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def crawl_cmd(
    beer_name: str,
    db_path: Optional[Path],
    force: bool,
    no_cache: bool,
    headed: bool,
    verbose: bool,
    as_json: bool,
) -> None:
    """Combined: beer name → URL → metadata; store both in SQLite."""
    _setup_logging(verbose)
    code = asyncio.run(
        _crawl(beer_name, db_path, force=force, use_cache=not no_cache, headless=not headed, as_json=as_json)
    )
    sys.exit(code)


async def _crawl(
    beer_name: str,
    db_path: Optional[Path],
    *,
    force: bool,
    use_cache: bool,
    headless: bool,
    as_json: bool,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_cache=use_cache)
    async with client:
        ref, meta = await service.crawl_beer(beer_name, force=force)
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
        console.print(f"[bold green]URL[/bold green]     {ref.page_url}  (match {ref.match_score:.2f})")
        if meta:
            console.print(f"[bold green]Rating[/bold green]  {meta.rating_score}  ·  {meta.name} · {meta.brewery}")
        else:
            console.print("[yellow]Metadata fetch failed[/yellow]")
            return 2
    return 0


@main.command("batch")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@_db_option
@click.option("--force", is_flag=True)
@click.option("--headed", is_flag=True)
@click.option("-v", "--verbose", is_flag=True)
@click.option("--delay", default=1.5, show_default=True, help="Seconds between beers")
def batch_cmd(
    file: Path,
    db_path: Optional[Path],
    force: bool,
    headed: bool,
    verbose: bool,
    delay: float,
) -> None:
    """Crawl one beer name per line from FILE."""
    _setup_logging(verbose)
    names = [ln.strip() for ln in file.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    code = asyncio.run(_batch(names, db_path, force=force, headless=not headed, delay=delay))
    sys.exit(code)


async def _batch(
    names: list[str],
    db_path: Optional[Path],
    *,
    force: bool,
    headless: bool,
    delay: float,
) -> int:
    db = BeerDatabase(db_path)
    client = UntappdClient(headless=headless)
    service = CrawlerService(db, client, use_cache=True)
    ok = 0
    async with client:
        for i, name in enumerate(names, 1):
            console.print(f"[cyan]({i}/{len(names)})[/cyan] {name}")
            ref, meta = await service.crawl_beer(name, force=force)
            if ref and meta and meta.rating_score is not None:
                console.print(f"  → {meta.rating_score}  {ref.page_url}")
                ok += 1
            elif ref:
                console.print(f"  → URL only {ref.page_url}")
            else:
                console.print("  → [red]failed[/red]")
            if i < len(names):
                await asyncio.sleep(delay)
    console.print(f"Done: {ok}/{len(names)} with scores")
    return 0 if ok else 1


@main.command("list")
@_db_option
@click.option("--limit", default=50, show_default=True)
def list_cmd(db_path: Optional[Path], limit: int) -> None:
    """List cached beer metadata from SQLite."""
    db = BeerDatabase(db_path)
    rows = db.list_metadata(limit=limit)
    table = Table(title=f"Cached beers ({db.path})")
    table.add_column("Rating", justify="right")
    table.add_column("Name")
    table.add_column("Brewery")
    table.add_column("URL")
    for m in rows:
        table.add_row(
            f"{m.rating_score:.2f}" if m.rating_score is not None else "—",
            m.name or "—",
            m.brewery or "—",
            m.page_url,
        )
    console.print(table)
    console.print(db.stats())


@main.command("init-db")
@_db_option
def init_db_cmd(db_path: Optional[Path]) -> None:
    """Create SQLite schema."""
    db = BeerDatabase(db_path)
    console.print(f"Initialized {db.path}")
    console.print(db.stats())


if __name__ == "__main__":
    main()
