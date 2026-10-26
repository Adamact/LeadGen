#!/usr/bin/env python3
"""
LeadGen — Scrape Allabolag.se by SNI code and enrich with web-searched contact info.

Usage:
    python main.py <sni_code> [options]

Examples:
    python main.py 62100                           # Software companies
    python main.py 62100 --pages 3 --output leads.csv
    python main.py 70200 --format json --no-enrich
    python main.py 62100 --details                 # Also fetch detail pages
    python main.py 62100 --contact-format columns  # One row per company
    python main.py 62100 --min-confidence 0.5      # Only high-quality leads
"""

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from scraper import scrape_by_sni
from enricher import enrich_companies
from export import export_leads, load_existing_org_numbers
from models import Lead
from logger import log

console = Console()


def print_summary(leads: list[Lead]):
    """Print a summary table of the leads."""
    table = Table(title="Lead Results", show_lines=True)
    table.add_column("Name", style="cyan", max_width=30)
    table.add_column("Org Nr", style="dim")
    table.add_column("Location")
    table.add_column("Website", style="blue", max_width=30)
    table.add_column("Best Contact", style="green", max_width=25)
    table.add_column("Title", max_width=20)
    table.add_column("Phone")
    table.add_column("#", justify="right")
    table.add_column("Conf", justify="right")

    for lead in leads[:50]:
        best = lead.contacts[0] if lead.contacts else None
        table.add_row(
            lead.name,
            lead.org_number,
            lead.location,
            lead.website[:30] if lead.website else "",
            best.name if best else "",
            best.title if best else "",
            best.phone if best else "",
            str(len(lead.contacts)),
            f"{lead.confidence:.2f}" if lead.confidence else "",
        )

    console.print(table)
    if len(leads) > 50:
        console.print(f"  ... and {len(leads) - 50} more (see export file)")

    # Stats
    total_contacts = sum(len(lead.contacts) for lead in leads)
    with_phone = sum(1 for lead in leads for c in lead.contacts if c.phone)
    with_email = sum(1 for lead in leads for c in lead.contacts if c.email)
    mobile_phones = sum(1 for lead in leads for c in lead.contacts if c.phone_type == "mobile")
    personal_emails = sum(1 for lead in leads for c in lead.contacts if c.email_type == "personal")

    console.print(f"\n[bold]Stats:[/] {total_contacts} contacts across {len(leads)} companies")
    console.print(f"  Phones: {with_phone} ({mobile_phones} mobile)")
    console.print(f"  Emails: {with_email} ({personal_emails} personal)")


async def run(args: argparse.Namespace):
    """Main pipeline: scrape → enrich → export."""
    log.info("=== LeadGen run started: sni=%s pages=%d enrich=%s details=%s output=%s format=%s ===",
             args.sni_code, args.pages, args.enrich, args.details, args.output, args.format)
    console.print(f"\n[bold]LeadGen[/] — SNI code: [cyan]{args.sni_code}[/]\n")

    # Step 1: Scrape
    companies = await scrape_by_sni(
        sni_code=args.sni_code,
        max_pages=args.pages,
        fetch_details=args.details,
        employees_from=args.employees_from,
        employees_to=args.employees_to,
        revenue_from=args.revenue_from,
        revenue_to=args.revenue_to,
        profit_from=args.profit_from,
        profit_to=args.profit_to,
    )

    if not companies:
        log.warning("No companies found for SNI %s — exiting", args.sni_code)
        console.print(
            "[red]No companies found. Check the SNI code and try again.[/]\n"
            "  Allabolag uses SNI 2025 codes; many SNI 2007 codes were renumbered "
            "(e.g. software development 62010 -> 62100)."
        )
        sys.exit(1)

    log.info("Scrape complete: %d companies", len(companies))

    # Skip companies that already exist in the output file
    existing_orgs = load_existing_org_numbers(args.output, args.format)
    if existing_orgs:
        before = len(companies)
        companies = [c for c in companies if c.org_number not in existing_orgs]
        skipped = before - len(companies)
        if skipped:
            console.print(
                f"  Skipped [yellow]{skipped}[/] companies already in {args.output} "
                f"([green]{len(companies)}[/] new)"
            )
            log.info("Skipped %d existing companies, %d new to enrich", skipped, len(companies))

        if not companies:
            console.print("[green]All companies already in output file. Nothing new to do.[/]")
            return []

    # Step 2: Enrich
    if args.enrich:
        leads = await enrich_companies(companies)
    else:
        leads = [Lead.from_company(c) for c in companies]

    # Step 3: Export (append to existing file if it has data)
    output_path = export_leads(
        leads,
        args.output,
        args.format,
        contact_format=args.contact_format,
        min_confidence=args.min_confidence,
        max_contacts=args.max_contacts,
        append=bool(existing_orgs),
    )
    log.info("Exported %d leads to %s", len(leads), output_path)
    console.print(f"\n[bold green]Exported {len(leads)} leads to:[/] {output_path}")

    # Summary
    print_summary(leads)

    # API cost summary
    if args.enrich and args.ai:
        from ai import usage_tracker
        console.print("\n[bold]API Usage:[/]")
        console.print(usage_tracker.summary())

    log.info("=== LeadGen run finished ===")
    return leads


def main():
    parser = argparse.ArgumentParser(
        description="Scrape Allabolag.se by SNI code and generate leads with contact info.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "sni_code",
        help="SNI code to search (e.g., 62100 for software development)",
    )
    parser.add_argument(
        "--pages", "-p",
        type=int,
        default=5,
        help="Maximum number of result pages to scrape (default: 5)",
    )
    parser.add_argument(
        "--output", "-o",
        default="leads.csv",
        help="Output file path (default: leads.csv)",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["csv", "json"],
        default="csv",
        help="Output format (default: csv)",
    )
    parser.add_argument(
        "--employees-from",
        type=int,
        default=20,
        help="Minimum number of employees (default: 20)",
    )
    parser.add_argument(
        "--employees-to",
        type=int,
        default=150,
        help="Maximum number of employees (default: 150)",
    )
    parser.add_argument(
        "--revenue-from",
        type=int,
        default=100,
        help="Minimum revenue in thousand SEK (default: 100)",
    )
    parser.add_argument(
        "--revenue-to",
        type=int,
        default=302_609_000,
        help="Maximum revenue in thousand SEK (default: 302609000)",
    )
    parser.add_argument(
        "--profit-from",
        type=int,
        default=100,
        help="Minimum operating profit in thousand SEK (default: 100)",
    )
    parser.add_argument(
        "--profit-to",
        type=int,
        default=85_733_000,
        help="Maximum operating profit in thousand SEK (default: 85733000)",
    )
    parser.add_argument(
        "--no-enrich",
        dest="enrich",
        action="store_false",
        default=True,
        help="Skip web enrichment (only scrape Allabolag)",
    )
    parser.add_argument(
        "--details", "-d",
        action="store_true",
        default=False,
        help="Fetch each company's detail page for extra data (slower)",
    )
    parser.add_argument(
        "--no-ai",
        dest="ai",
        action="store_false",
        default=True,
        help="Disable AI-powered extraction (use regex fallback only, no API key needed)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="Run browser in visible mode (useful for debugging)",
    )
    # New multi-contact options
    parser.add_argument(
        "--contact-format",
        choices=["rows", "columns"],
        default="rows",
        help="CSV contact layout: 'rows' = one row per contact, 'columns' = one row per company (default: rows)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.0,
        help="Minimum confidence score to include a contact (0.0-1.0, default: 0.0)",
    )
    parser.add_argument(
        "--max-contacts",
        type=int,
        default=5,
        help="Max contacts per company in 'columns' mode (default: 5)",
    )
    parser.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        default=True,
        help="Bypass enrichment cache (re-fetch everything)",
    )
    parser.add_argument(
        "--enrichment-timeout",
        type=int,
        default=60,
        help="Max seconds per company enrichment before skipping (default: 60)",
    )

    args = parser.parse_args()

    # Update config from CLI args
    import config
    config.HEADLESS = args.headless
    config.AI_ENABLED = args.ai
    config.CACHE_ENABLED = args.cache
    config.ENRICHMENT_TIMEOUT = args.enrichment_timeout

    if args.ai:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            console.print(
                "[yellow]Warning:[/] ANTHROPIC_API_KEY not set. "
                "AI features will fail.\n"
                "  Set it:  export ANTHROPIC_API_KEY=sk-ant-...\n"
                f"  Or run:  python main.py {args.sni_code} --no-ai\n"
            )

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
