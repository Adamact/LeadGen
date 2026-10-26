"""Enrich scraped companies with contact info via web searches."""

import asyncio
import random
import re
import time
from difflib import SequenceMatcher

from ddgs import DDGS
from ddgs.exceptions import DDGSException
from rich.console import Console
from rich.markup import escape

# Runtime toggles (AI_ENABLED, CACHE_ENABLED, ENRICHMENT_TIMEOUT) are read via `config.` because main.py sets them from CLI flags
import config
from config import (
    ENRICHMENT_DELAY,
    MAX_SEARCH_RESULTS_PER_COMPANY,
    SEARCH_BACKOFF,
    SEARCH_RETRIES,
    SKIP_IF_NO_WEBSITE,
    SKIP_WEBSITE_IF_GOOD_SEARCH,
)
from models import Company, Contact, Lead
from logger import log

console = Console()

# Common email patterns
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# Phone patterns (Swedish format)
PHONE_RE = re.compile(
    r"(?:\+46|0)[\s\-]?\d{1,3}[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2}"
)

# LinkedIn company URL
LINKEDIN_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9\-_%]+"
)

# Domains that are NOT the company's own website — directories, databases, social media
SKIP_DOMAINS = {
    # Social media
    "linkedin.com", "facebook.com", "twitter.com", "instagram.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    # Search engines
    "google.com", "google.se", "duckduckgo.com", "bing.com",
    # Swedish company databases / directories
    "allabolag.se", "hitta.se", "eniro.se", "ratsit.se",
    "bolagsfakta.se", "allbiz.se", "foretagsfakta.se",
    "proff.se", "upplysning.se", "merinfo.se", "kreditkollen.se",
    "solidinfo.se", "bisnode.se", "infotorg.se", "bolagsverket.se",
    "largestcompanies.se", "europages.com", "kompass.com",
    "dnb.com", "vainu.com", "leadfeeder.com",
    "infobel.com", "cylex.se", "gulex.se", "118100.se", "bizzdo.se",
    "uc.se", "syna.se", "northdata.com", "northdata.de", "opencorporates.com",
    # B2B data / people-search sites
    "crunchbase.com", "zoominfo.com", "rocketreach.co", "apollo.io", "lusha.com",
    # Generic directories / review sites
    "wikipedia.org", "trustpilot.com", "glassdoor.com",
    "indeed.com", "monster.se", "jobba.se",
}


def _domain(url: str) -> str:
    """Return the lowercase host of a URL without a leading www."""
    return re.sub(r"https?://(www\.)?", "", url).split("/")[0].split(":")[0].lower()


def _is_skip_domain(url: str) -> bool:
    """True if the URL is on a directory/social domain (or a subdomain of one)."""
    domain = _domain(url)
    return any(domain == sd or domain.endswith("." + sd) for sd in SKIP_DOMAINS)


# ---------------------------------------------------------------------------
# Phone classification
# ---------------------------------------------------------------------------

def classify_phone(phone: str) -> str:
    """Classify a Swedish phone number as mobile, landline, or unknown."""
    normalized = re.sub(r"[\s\-\(\)]", "", phone)
    if normalized.startswith("+46"):
        normalized = "0" + normalized[3:]
    if re.match(r"^07[0-9]", normalized):
        return "mobile"
    if re.match(r"^0[1-9][0-9]", normalized):
        return "landline"
    return "unknown"


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

def compute_confidence(contact: Contact) -> float:
    """Compute a deterministic confidence score for a contact."""
    score = 0.0

    if contact.name:
        score += 0.2
    if contact.title:
        score += 0.1
    if contact.email:
        if contact.email_type == "personal" or _is_personal_email(contact.email):
            score += 0.25
        elif contact.email_type in ("role", "generic"):
            score += 0.05
        else:
            score += 0.1
    if contact.phone:
        phone_class = contact.phone_type or classify_phone(contact.phone)
        if phone_class == "mobile":
            score += 0.25
        elif phone_class in ("direct", "landline") and contact.phone_type != "switchboard":
            score += 0.15
    if contact.source == "website":
        score += 0.15
    if contact.linkedin:
        score += 0.1

    return min(score, 1.0)


def _is_personal_email(email: str) -> bool:
    """Heuristic: does the email look like firstname.lastname@?"""
    local = email.split("@")[0].lower()
    generic = {"info", "kontakt", "contact", "admin", "support", "hello",
               "sales", "office", "vd", "ceo", "hr", "jobb", "faktura"}
    if local in generic:
        return False
    return "." in local and len(local) > 4


# ---------------------------------------------------------------------------
# Contact merging / deduplication
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Normalize a name for comparison."""
    return re.sub(r"[^a-zåäö ]", "", name.lower().strip())


def _names_match(a: str, b: str) -> bool:
    """Check if two names likely refer to the same person."""
    na, nb = _normalize_name(a), _normalize_name(b)
    if not na or not nb:
        return False
    return SequenceMatcher(None, na, nb).ratio() > 0.8


def merge_contacts(contacts: list[Contact]) -> list[Contact]:
    """Deduplicate contacts by name, merging fields. Website source wins."""
    merged: list[Contact] = []

    for contact in contacts:
        found = False
        for existing in merged:
            if _names_match(existing.name, contact.name):
                # Merge: prefer website source, fill in blanks
                if contact.source == "website" and existing.source != "website":
                    # Website contact takes priority for all fields
                    existing.name = contact.name or existing.name
                    existing.title = contact.title or existing.title
                    existing.email = contact.email or existing.email
                    existing.phone = contact.phone or existing.phone
                    existing.phone_type = contact.phone_type or existing.phone_type
                    existing.email_type = contact.email_type or existing.email_type
                    existing.linkedin = contact.linkedin or existing.linkedin
                    existing.source = "website"
                else:
                    # Fill blanks from this source
                    existing.email = existing.email or contact.email
                    existing.phone = existing.phone or contact.phone
                    existing.phone_type = existing.phone_type or contact.phone_type
                    existing.email_type = existing.email_type or contact.email_type
                    existing.linkedin = existing.linkedin or contact.linkedin
                    existing.title = existing.title or contact.title
                found = True
                break

        if not found:
            merged.append(Contact(
                name=contact.name,
                title=contact.title,
                email=contact.email,
                phone=contact.phone,
                phone_type=contact.phone_type,
                email_type=contact.email_type,
                linkedin=contact.linkedin,
                source=contact.source,
            ))

    return merged


# ---------------------------------------------------------------------------
# Regex-based extraction (fallback)
# ---------------------------------------------------------------------------

def _extract_emails(text: str) -> list[str]:
    """Extract email addresses, filtering out common false positives."""
    emails = EMAIL_RE.findall(text)
    blocked = {"example.com", "example.org", "sentry.io", "w3.org", "schema.org", "googleapis.com"}
    return [e for e in emails if not any(b in e for b in blocked)]


def _extract_phones(text: str) -> list[str]:
    return PHONE_RE.findall(text)


def _extract_linkedin(text: str) -> str:
    match = LINKEDIN_RE.search(text)
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _gather_search_results(company: Company) -> list[dict]:
    """Run DuckDuckGo searches and return raw results."""
    queries = [
        f'"{company.name}" VD CEO kontakt telefon',
        f'"{company.name}" site:linkedin.com',
        f'"{company.name}" ledning ekonomichef CFO kontakt',
    ]

    all_results = []

    with DDGS() as ddgs:
        for query in queries:
            all_results.extend(_search(ddgs, query))
            time.sleep(random.uniform(*ENRICHMENT_DELAY))

    return all_results


def _search(ddgs: DDGS, query: str) -> list[dict]:
    """
    Run one search, backing off and retrying when the backend fails.

    When throttled, ddgs often reports "No results found" for queries that do have
    results, so empty/failed searches are retried after a pause before giving up.
    """
    for attempt in range(SEARCH_RETRIES + 1):
        log.debug("DuckDuckGo query (attempt %d): %s", attempt + 1, query)
        try:
            results = list(ddgs.text(query, region="se-sv", max_results=MAX_SEARCH_RESULTS_PER_COMPANY))
            log.debug("Query returned %d results", len(results))
            return results
        except DDGSException as e:
            log.debug("Search failed for query '%s': %s", query, e, exc_info=True)
            if attempt < SEARCH_RETRIES:
                time.sleep(SEARCH_BACKOFF * (attempt + 1))
            else:
                log.info("Giving up on query '%s' after %d attempts: %s", query, attempt + 1, e)
    return []


def _find_website(search_results: list[dict]) -> str:
    """Extract the company's own website URL from search results."""
    for r in search_results:
        url = r.get("href", "")
        if url and not _is_skip_domain(url):
            return url
    return ""


# ---------------------------------------------------------------------------
# AI enrichment
# ---------------------------------------------------------------------------

def _enrich_with_ai(company: Company, search_results: list[dict]) -> dict:
    """Use Sonnet to extract contact info from search results."""
    from ai import enrich_lead_from_search

    log.info("Using AI enrichment for '%s'", company.name)
    try:
        return enrich_lead_from_search(
            company_name=company.name,
            org_number=company.org_number,
            location=company.location,
            search_results=search_results,
        )
    except Exception as e:
        log.error("AI enrichment failed for '%s': %s — falling back to regex", company.name, e, exc_info=True)
        return {}


def _enrich_with_regex(company: Company, search_results: list[dict]) -> dict:
    """Regex-based fallback extraction from search results."""
    info = {
        "website": "",
        "linkedin": "",
        "description": "",
        "contacts": [],
    }

    all_text = " ".join(
        f"{r.get('title', '')} {r.get('body', '')} {r.get('href', '')}" for r in search_results
    )

    # Extract website — only the company's own domain
    info["website"] = _find_website(search_results)

    # Extract a generic contact from regex matches
    emails = _extract_emails(all_text)
    phones = _extract_phones(all_text)
    linkedin = _extract_linkedin(all_text)

    if linkedin:
        info["linkedin"] = linkedin

    # Create a generic contact from whatever we found
    if emails or phones:
        email = emails[0] if emails else ""
        phone = phones[0] if phones else ""
        info["contacts"] = [Contact(
            email=email,
            phone=phone,
            phone_type=classify_phone(phone) if phone else "",
            email_type="personal" if _is_personal_email(email) else "generic" if email else "",
            source="search",
        )]

    if search_results:
        info["description"] = search_results[0].get("body", "")[:300]

    log.info("Regex enrichment for '%s': website=%s, contacts=%d, linkedin=%s",
             company.name, bool(info["website"]), len(info["contacts"]), bool(info["linkedin"]))
    return info


# ---------------------------------------------------------------------------
# Website scraping
# ---------------------------------------------------------------------------

def _is_company_website(url: str) -> bool:
    """Check that a URL is the company's own website, not a directory or database."""
    return not _is_skip_domain(url)


def _scrape_company_website(company: Company, website_url: str) -> dict:
    """Scrape the company's actual website and extract contacts with AI."""
    from ai import scrape_website_pages, extract_contacts_from_website

    if not _is_company_website(website_url):
        log.info("Skipping directory site for '%s': %s", company.name, website_url)
        console.print(f"    Skipping [yellow]{website_url}[/] (directory, not company site)")
        return {}

    log.info("Scraping website for '%s': %s", company.name, website_url)
    console.print(f"    Scraping [blue]{website_url}[/]...")

    try:
        website_text = scrape_website_pages(website_url)
    except Exception as e:
        log.error("Website scrape failed for '%s': %s", company.name, e)
        return {}

    if not website_text:
        log.warning("No text content from website %s", website_url)
        return {}

    try:
        return extract_contacts_from_website(company.name, website_text)
    except Exception as e:
        log.error("AI website extraction failed for '%s': %s", company.name, e, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Email inference
# ---------------------------------------------------------------------------

def _try_infer_emails(contacts: list[Contact], known_emails: list[str]) -> list[Contact]:
    """Try to infer personal emails for contacts that don't have one."""
    from ai import infer_personal_email

    # Determine the company email domain from any known email
    domain = ""
    for email in known_emails:
        if "@" in email:
            d = email.split("@")[1].lower()
            generic_domains = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "live.se"}
            if d not in generic_domains:
                domain = d
                break

    if not domain:
        return contacts

    for contact in contacts:
        if contact.email or not contact.name:
            continue  # never overwrite or relabel an email we actually found

        candidates = infer_personal_email(contact.name, domain)
        if candidates:
            # Use the most common pattern as a guess
            contact.email = candidates[0]
            contact.email_type = "personal"
            contact.source = contact.source or "inferred"
            log.info("Inferred email for %s: %s", contact.name, candidates[0])

    return contacts


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def _search_and_enrich(company: Company) -> dict:
    """Search the web, find website, scrape it, and extract all contacts."""
    # Check cache first
    if config.CACHE_ENABLED and company.org_number:
        from cache import get_cached
        cached = get_cached(company.org_number, "enrichment")
        if cached is not None:
            log.info("Using cached enrichment for '%s'", company.name)
            console.print("    [dim]Using cached result[/]")
            # Reconstruct Contact objects from cached dicts
            contacts = []
            for c in cached.get("contacts", []):
                if isinstance(c, dict):
                    contacts.append(Contact(**c))
                else:
                    contacts.append(c)
            cached["contacts"] = contacts
            return cached

    search_results = _gather_search_results(company)

    if not search_results:
        log.warning("No search results for '%s'", company.name)
        return {}

    # Step 1: Get contacts from search results (AI or regex)
    if config.AI_ENABLED:
        info = _enrich_with_ai(company, search_results)
        if not info or not info.get("contacts"):
            info = _enrich_with_regex(company, search_results)
    else:
        info = _enrich_with_regex(company, search_results)

    # Step 2: Find the company's own website (not directories)
    website = info.get("website", "") or _find_website(search_results)
    if website and not _is_company_website(website):
        log.info("Discarding directory URL for '%s': %s", company.name, website)
        website = ""
    if website:
        info["website"] = website
    else:
        info["website"] = ""

    # Collect all contacts from all sources
    all_contacts: list[Contact] = list(info.get("contacts", []))

    # Skip deep enrichment if no real website found
    if not website and SKIP_IF_NO_WEBSITE:
        log.info("No company website for '%s', skipping deep enrichment", company.name)
        console.print("    [yellow]No website found — skipping deep enrichment[/]")
        # Still score whatever we got from search
        for contact in all_contacts:
            contact.confidence = compute_confidence(contact)
        all_contacts.sort(key=lambda c: c.confidence, reverse=True)
        info["contacts"] = all_contacts
        return info

    # Skip expensive website scrape if search already yielded quality contacts
    has_quality_contact = SKIP_WEBSITE_IF_GOOD_SEARCH and any(
        c.email and _is_personal_email(c.email) and c.phone and classify_phone(c.phone) == "mobile"
        for c in all_contacts
    )
    if has_quality_contact:
        log.info("Search already found quality contact for '%s', skipping website scrape", company.name)
        console.print("    [green]Good contact from search — skipping website scrape[/]")

    # Step 3: Scrape the actual website for contacts (AI-only feature)
    all_emails = []
    if config.AI_ENABLED and website and not has_quality_contact:
        site_data = _scrape_company_website(company, website)

        if site_data.get("contacts"):
            all_contacts.extend(site_data["contacts"])

        if site_data.get("description"):
            info["description"] = site_data["description"]

        all_emails = site_data.get("all_emails", [])

    # Step 4: Merge/deduplicate contacts
    merged = merge_contacts(all_contacts)

    # Step 5: Validate phone classifications
    for contact in merged:
        if contact.phone:
            python_class = classify_phone(contact.phone)
            if python_class == "mobile":
                contact.phone_type = "mobile"
            elif not contact.phone_type:
                contact.phone_type = python_class

    # Step 6: Try to infer personal emails for contacts missing them
    if config.AI_ENABLED and all_emails:
        merged = _try_infer_emails(merged, all_emails)

    # Step 7: Compute confidence scores
    for contact in merged:
        contact.confidence = compute_confidence(contact)

    # Sort by confidence descending
    merged.sort(key=lambda c: c.confidence, reverse=True)

    info["contacts"] = merged

    # Cache the result
    if config.CACHE_ENABLED and company.org_number:
        from cache import set_cached
        cache_data = dict(info)
        cache_data["contacts"] = [c.to_dict() for c in merged]
        set_cached(company.org_number, "enrichment", cache_data)

    return info


async def enrich_companies(
    companies: list[Company],
) -> list[Lead]:
    """
    Enrich a list of companies with web-searched contact info.
    Returns list of Lead objects with multiple contacts per lead.
    """
    leads: list[Lead] = []
    total = len(companies)

    mode = "AI + web search" if config.AI_ENABLED else "web search (regex)"
    console.print(f"\n[bold blue]Enriching {total} companies via {mode}...[/]")

    for i, company in enumerate(companies):
        console.print(f"  [{i+1}/{total}] Searching for [cyan]{company.name}[/]...")

        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(_search_and_enrich, company),
                timeout=config.ENRICHMENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Enrichment timed out for '%s' after %ds", company.name, config.ENRICHMENT_TIMEOUT)
            console.print(f"    [yellow]Skipped (timeout after {config.ENRICHMENT_TIMEOUT}s)[/]")
            info = {}
        except Exception as e:
            log.error("Enrichment failed for '%s': %s", company.name, e, exc_info=True)
            console.print(f"    [red]Enrichment failed: {e}[/]")
            info = {}

        lead = Lead.from_company(company)
        lead.website = info.get("website", "")
        lead.linkedin = info.get("linkedin", "")
        lead.description = info.get("description", "")
        lead.contacts = info.get("contacts", [])

        # Populate legacy flat fields from best contact
        lead.set_best_contact()

        contact_count = len(lead.contacts)
        best = lead.contacts[0] if lead.contacts else None
        if best:
            who = " ".join(filter(None, [best.name, f"({best.title})" if best.title else ""]))
            who = who or best.email or best.phone or "unnamed"
            # escape(): rich would otherwise treat "[...]" in names or the score as markup
            console.print(
                f"    Found [green]{contact_count}[/] contact(s) — "
                f"best: {escape(who)} "
                f"{escape(f'[confidence: {best.confidence:.2f}]')}"
            )
        else:
            console.print("    Found [yellow]0[/] contacts")

        leads.append(lead)

    console.print(f"\n[bold green]Enrichment complete: {len(leads)} leads[/]")
    return leads
