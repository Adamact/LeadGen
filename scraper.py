"""Scraper for Allabolag.se — fetches companies by SNI code."""

import asyncio
import random
import re
from urllib.parse import unquote

from playwright.async_api import async_playwright, Page
from bs4 import BeautifulSoup
from rich.console import Console

from config import (
    AI_ENABLED,
    ALLABOLAG_BASE_URL,
    HEADLESS,
    REQUEST_DELAY,
    PAGE_LOAD_TIMEOUT,
)
from models import Company
from logger import log

console = Console()

SEGMENTERING_URL = f"{ALLABOLAG_BASE_URL}/segmentering"


async def _random_delay():
    """Human-like delay between requests."""
    delay = random.uniform(*REQUEST_DELAY)
    log.debug("Sleeping %.1fs between requests", delay)
    await asyncio.sleep(delay)


async def _launch_browser() -> tuple:
    """Launch a Playwright browser with stealth-ish settings."""
    log.info("Launching Playwright Chromium (headless=%s)", HEADLESS)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=HEADLESS,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="sv-SE",
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    log.info("Browser launched successfully")
    return pw, browser, context


async def _handle_cookie_consent(page: Page):
    """Accept cookie consent dialog if present."""
    try:
        consent_btn = await page.query_selector(
            'button:has-text("GODKÄNN"), button:has-text("Acceptera"), '
            'button:has-text("Godkänn"), button[id*="accept"], button[class*="consent"]'
        )
        if consent_btn:
            log.debug("Cookie consent button found, clicking")
            await consent_btn.click()
            await asyncio.sleep(2)
    except Exception as e:
        log.debug("Cookie consent handling: %s", e)


def _parse_with_ai(html: str, sni_code: str) -> list[Company]:
    """Use Sonnet to extract companies from HTML (primary parser when AI is enabled)."""
    from ai import extract_companies_from_html

    log.info("Using AI parser for company extraction")
    try:
        raw = extract_companies_from_html(html, sni_code)
    except Exception as e:
        log.error("AI parser failed: %s — falling back to regex parser", e, exc_info=True)
        return []

    companies = []
    for item in raw:
        name = item.get("name", "").strip()
        org = item.get("org_number", "").strip()
        if not name or not org:
            continue
        url = item.get("allabolag_url", "")
        if url and not url.startswith("http"):
            url = f"{ALLABOLAG_BASE_URL}{url}" if url.startswith("/") else ""
        companies.append(Company(
            name=name,
            org_number=org,
            sni_code=sni_code,
            status=item.get("status", ""),
            revenue=item.get("revenue", ""),
            employees=item.get("employees", ""),
            location=item.get("location", ""),
            allabolag_url=url,
        ))

    log.info("AI parser returned %d companies", len(companies))
    return companies


def _parse_company_list(html: str, sni_code: str) -> list[Company]:
    """Parse the segmentering page and extract companies."""

    # Try AI parser first when enabled
    if AI_ENABLED:
        companies = _parse_with_ai(html, sni_code)
        if companies:
            return companies
        log.warning("AI parser returned 0 results, falling back to regex parser")

    soup = BeautifulSoup(html, "lxml")
    companies = []

    # Segmentering links: /foretag/slug/-/ORG_NUMBER or /foretag/slug/city/category/HASH
    company_links = soup.select("a[href*='/foretag/']")
    log.debug("Found %d company links", len(company_links))

    seen_orgs = set()
    for link in company_links:
        href = link.get("href", "")
        if not href:
            continue

        name = link.get_text(strip=True)
        if not name or len(name) < 2:
            continue

        # Try to extract org number from URL: /foretag/slug/-/5568992761
        org_number = ""
        org_match = re.search(r"/(\d{10})$", href)
        if org_match:
            org_number = org_match.group(1)

        # Also check parent text for org numbers with dashes
        if not org_number:
            parent = link
            for _ in range(5):
                parent = parent.parent
                if parent is None:
                    break
                parent_text = parent.get_text(" ", strip=True)
                m = re.search(r"\b(\d{6}-\d{4})\b", parent_text)
                if m:
                    org_number = m.group(1).replace("-", "")
                    break

        # Deduplicate by org number or URL
        key = org_number or href
        if key in seen_orgs:
            continue
        seen_orgs.add(key)

        # Extract location from URL: /foretag/slug/CITY/-/org
        location = ""
        parts = href.strip("/").split("/")
        # Format: foretag / slug / city / -  / org
        if len(parts) >= 4:
            loc_part = parts[2] if parts[2] != "-" else ""
            if loc_part:
                location = unquote(loc_part).replace("-", " ").title()

        # Extract employees from nearby text
        employees = ""
        if link.parent:
            block_text = link.parent.get_text(" ", strip=True)
            emp_match = re.search(r"Anst.llda\s+(\d+)", block_text)
            if emp_match:
                employees = emp_match.group(1)

        # Extract revenue from nearby text
        revenue = ""
        if link.parent:
            block_text = link.parent.get_text(" ", strip=True)
            rev_match = re.search(r"Oms.ttning\s+\d{4}\s+([\d\s]+)", block_text)
            if rev_match:
                revenue = rev_match.group(1).strip()

        company = Company(
            name=name,
            org_number=org_number,
            sni_code=sni_code,
            location=location,
            employees=employees,
            revenue=revenue,
            allabolag_url=f"{ALLABOLAG_BASE_URL}{href}" if href.startswith("/") else href,
        )
        log.debug("Parsed: %s (org: %s, emp: %s, loc: %s)", name, org_number, employees, location)
        companies.append(company)

    # Fallback if nothing found
    if not companies:
        log.warning("Primary parser found 0 companies, trying fallback")
        companies = _parse_fallback(soup, sni_code)

    log.info("Parsed %d companies for SNI %s", len(companies), sni_code)
    return companies


def _parse_fallback(soup: BeautifulSoup, sni_code: str) -> list[Company]:
    """Fallback parser — scans text for org-number patterns."""
    companies = []
    seen = set()

    text = soup.get_text()
    org_matches = re.findall(r"\b(\d{6}-?\d{4})\b", text)
    log.debug("Fallback found %d org-number candidates", len(org_matches))

    for org_raw in org_matches:
        org_clean = org_raw.replace("-", "")
        if org_clean in seen:
            continue
        seen.add(org_clean)

        pattern = re.compile(re.escape(org_raw))
        element = soup.find(string=pattern)
        name = ""
        if element and element.parent:
            for parent in element.parents:
                heading = parent.find(["h2", "h3", "h4", "strong", "b"])
                if heading:
                    name = heading.get_text(strip=True)
                    break
                if parent.name in ("article", "div", "li", "tr"):
                    name = parent.get_text(" ", strip=True)[:100]
                    break

        if name:
            companies.append(Company(name=name, org_number=org_clean, sni_code=sni_code))

    return companies


def _parse_company_detail(html: str) -> dict:
    """Parse a single company detail page for extra information."""
    soup = BeautifulSoup(html, "lxml")
    info = {}

    addr_el = soup.select_one("[class*='address'], [data-field='address']")
    if addr_el:
        info["address"] = addr_el.get_text(strip=True)

    text = soup.get_text(" ", strip=True)

    rev_match = re.search(r"[Oo]msättning[:\s]+([0-9\s]+(?:tkr|mkr|TSEK|MSEK))", text)
    if rev_match:
        info["revenue"] = rev_match.group(1).strip()

    emp_match = re.search(r"[Aa]nställda[:\s]+(\d[\d\s\-–]*)", text)
    if emp_match:
        info["employees"] = emp_match.group(1).strip()

    loc_match = re.search(r"[Ss]äte[:\s]+([A-ZÅÄÖa-zåäö\s]+?)(?:\s{2}|\n|$)", text)
    if loc_match:
        info["location"] = loc_match.group(1).strip()

    if re.search(r"[Aa]ktiv[t]?\b", text):
        info["status"] = "Aktiv"

    return info


async def _scroll_to_load_all(page: Page, max_scrolls: int = 20):
    """Scroll down to trigger lazy loading of all results."""
    for i in range(max_scrolls):
        prev_height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            log.debug("No more content after scroll %d", i + 1)
            break
        log.debug("Scroll %d: height %d -> %d", i + 1, prev_height, new_height)


def _build_employee_windows(emp_from: int, emp_to: int) -> list[tuple[int, int]]:
    """
    Split an employee range into small windows to work around the 10-result
    paywall limit on the segmentering page. Each window yields up to 10
    different companies.
    """
    windows = []
    cursor = emp_from
    # Use small windows at the low end (more companies), larger at the high end
    while cursor < emp_to:
        if cursor < 30:
            step = 5
        elif cursor < 50:
            step = 10
        elif cursor < 100:
            step = 20
        else:
            step = 50
        window_end = min(cursor + step, emp_to)
        windows.append((cursor, window_end))
        cursor = window_end
    return windows


async def _scrape_one_window(
    page: Page,
    nace_code: str,
    sni_code: str,
    emp_from: int,
    emp_to: int,
    revenue_from: int | None,
    revenue_to: int | None,
    profit_from: int | None,
    profit_to: int | None,
    seen_orgs: set,
) -> list[Company]:
    """Scrape a single employee-range window. Returns new (unseen) companies."""
    params = [f"naceIndustry={nace_code}"]
    params.append(f"numEmployeesFrom={emp_from}")
    params.append(f"numEmployeesTo={emp_to}")
    if revenue_from is not None:
        params.append(f"revenueFrom={revenue_from}")
    if revenue_to is not None:
        params.append(f"revenueTo={revenue_to}")
    if profit_from is not None:
        params.append(f"profitFrom={profit_from}")
    if profit_to is not None:
        params.append(f"profitTo={profit_to}")

    url = f"{SEGMENTERING_URL}?{'&'.join(params)}"
    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(4)

    html = await page.content()
    companies = _parse_company_list(html, sni_code)

    # Deduplicate against already-seen orgs/URLs
    new_companies = []
    for c in companies:
        key = c.org_number or c.allabolag_url
        if key and key not in seen_orgs:
            seen_orgs.add(key)
            new_companies.append(c)

    return new_companies


async def scrape_by_sni(
    sni_code: str,
    max_pages: int = 5,
    fetch_details: bool = False,
    employees_from: int | None = None,
    employees_to: int | None = None,
    revenue_from: int | None = None,
    revenue_to: int | None = None,
    profit_from: int | None = None,
    profit_to: int | None = None,
) -> list[Company]:
    """
    Scrape companies from Allabolag.se segmentering page.

    The segmentering page only shows ~10 companies per request (paywall).
    To get more results, we split the employee range into small windows
    and make multiple requests, collecting unique companies from each.
    """
    all_companies: list[Company] = []
    seen_orgs: set[str] = set()

    # Format SNI code with dot: 62010 -> 62.010
    nace_code = f"{sni_code[:2]}.{sni_code[2:]}" if len(sni_code) > 2 and "." not in sni_code else sni_code

    emp_from = employees_from if employees_from is not None else 20
    emp_to = employees_to if employees_to is not None else 150

    windows = _build_employee_windows(emp_from, emp_to)

    log.info("Starting scrape: sni=%s, employee range %d-%d, %d windows",
             sni_code, emp_from, emp_to, len(windows))
    console.print(f"[bold blue]Scraping SNI code: {sni_code}[/]")
    console.print(f"  Employees: {emp_from}-{emp_to} ({len(windows)} windows to bypass 10-result limit)")

    pw, browser, context = await _launch_browser()

    try:
        page = await context.new_page()
        page.set_default_timeout(PAGE_LOAD_TIMEOUT)

        # Accept cookies on the homepage first
        await page.goto(ALLABOLAG_BASE_URL, wait_until="domcontentloaded")
        await asyncio.sleep(2)
        await _handle_cookie_consent(page)

        # Scrape each employee window
        for i, (w_from, w_to) in enumerate(windows):
            console.print(f"  Window {i+1}/{len(windows)}: employees {w_from}-{w_to}...", end=" ")

            new = await _scrape_one_window(
                page, nace_code, sni_code,
                w_from, w_to,
                revenue_from, revenue_to,
                profit_from, profit_to,
                seen_orgs,
            )
            all_companies.extend(new)
            console.print(f"[green]+{len(new)}[/] new")

            if i < len(windows) - 1:
                await _random_delay()

        console.print(f"  Total unique: [green]{len(all_companies)}[/]")

        # Optionally fetch detail pages
        if fetch_details and all_companies:
            console.print(f"\n[bold blue]Fetching details for {len(all_companies)} companies...[/]")
            for i, company in enumerate(all_companies):
                if not company.allabolag_url:
                    continue
                console.print(f"  [{i+1}/{len(all_companies)}] {company.name}")
                try:
                    await page.goto(company.allabolag_url, wait_until="domcontentloaded")
                    await _random_delay()
                    detail_html = await page.content()
                    details = _parse_company_detail(detail_html)
                    if details.get("address"):
                        company.address = details["address"]
                    if details.get("revenue") and not company.revenue:
                        company.revenue = details["revenue"]
                    if details.get("employees") and not company.employees:
                        company.employees = details["employees"]
                    if details.get("location") and not company.location:
                        company.location = details["location"]
                    if details.get("status") and not company.status:
                        company.status = details["status"]
                except Exception as e:
                    log.error("Error fetching details for %s: %s", company.name, e, exc_info=True)
                    console.print(f"    [red]Error fetching details: {e}[/]")

    except Exception as e:
        log.critical("Scraper crashed: %s", e, exc_info=True)
        raise

    finally:
        log.info("Closing browser")
        await browser.close()
        await pw.stop()

    log.info("Scrape complete: %d companies for SNI %s", len(all_companies), sni_code)
    console.print(f"\n[bold green]Total companies scraped: {len(all_companies)}[/]")
    return all_companies
