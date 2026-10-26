"""AI-powered extraction using Claude via the Anthropic API."""

import json
import os
import re
from urllib.parse import urljoin, urlparse

import anthropic
import httpx
from bs4 import BeautifulSoup
from rich.console import Console

from config import (
    ANTHROPIC_MODEL,
    ANTHROPIC_MODEL_LIGHT,
    AI_EFFORT,
    AI_MAX_TOKENS,
    AI_SEARCH_MAX_TOKENS,
    AI_WEBSITE_MAX_TOKENS,
    WEBSITE_SCRAPE_TIMEOUT,
    WEBSITE_SCRAPE_PATHS,
    MAX_WEBSITE_PAGES,
)
from models import Contact
from logger import log

console = Console()

_client: anthropic.Anthropic | None = None

# ---------------------------------------------------------------------------
# API usage tracking
# ---------------------------------------------------------------------------

class APIUsageTracker:
    """Track API calls, tokens, and estimated cost per model."""

    # USD per million tokens (Sonnet 5.5 / Haiku 5.5, Oct 2026; Haiku rate is for prompts <=100K)
    MODEL_PRICING = {
        "sonnet": {"input": 2.00, "output": 10.00},
        "haiku": {"input": 0.10, "output": 0.50},
    }

    def __init__(self):
        self.calls = 0
        self.calls_by_type: dict[str, int] = {}
        # Track tokens per model tier for accurate cost
        self.usage_by_tier: dict[str, dict[str, int]] = {}

    def _tier(self, model: str) -> str:
        """Map a model ID to a pricing tier."""
        if "haiku" in model:
            return "haiku"
        return "sonnet"

    def record(self, response, call_type: str = "unknown", model: str = ""):
        """Record usage from an API response."""
        self.calls += 1
        self.calls_by_type[call_type] = self.calls_by_type.get(call_type, 0) + 1
        tier = self._tier(model)
        if tier not in self.usage_by_tier:
            self.usage_by_tier[tier] = {"input_tokens": 0, "output_tokens": 0}
        if hasattr(response, "usage"):
            self.usage_by_tier[tier]["input_tokens"] += response.usage.input_tokens
            self.usage_by_tier[tier]["output_tokens"] += response.usage.output_tokens

    @property
    def input_tokens(self) -> int:
        return sum(u["input_tokens"] for u in self.usage_by_tier.values())

    @property
    def output_tokens(self) -> int:
        return sum(u["output_tokens"] for u in self.usage_by_tier.values())

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost(self) -> float:
        """Estimated cost in USD, accounting for per-model pricing."""
        total = 0.0
        for tier, usage in self.usage_by_tier.items():
            pricing = self.MODEL_PRICING.get(tier, self.MODEL_PRICING["sonnet"])
            total += (usage["input_tokens"] / 1_000_000) * pricing["input"]
            total += (usage["output_tokens"] / 1_000_000) * pricing["output"]
        return total

    def summary(self) -> str:
        """Return a formatted summary string."""
        lines = [
            f"API calls: {self.calls}",
            f"  Input tokens:  {self.input_tokens:,}",
            f"  Output tokens: {self.output_tokens:,}",
            f"  Total tokens:  {self.total_tokens:,}",
            f"  Est. cost:     ${self.estimated_cost:.4f}",
        ]
        for tier, usage in sorted(self.usage_by_tier.items()):
            lines.append(f"  {tier}: {usage['input_tokens']:,} in / {usage['output_tokens']:,} out")
        if self.calls_by_type:
            lines.append("  Breakdown:")
            for ctype, count in sorted(self.calls_by_type.items()):
                lines.append(f"    {ctype}: {count}")
        return "\n".join(lines)


usage_tracker = APIUsageTracker()


def _get_client() -> anthropic.Anthropic:
    """Lazy-init the Anthropic client."""
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it or use --no-ai to skip AI features.\n"
                "  export ANTHROPIC_API_KEY=sk-ant-..."
            )
        _client = anthropic.Anthropic(api_key=api_key)
        log.info("Anthropic client initialized (model: %s)", ANTHROPIC_MODEL)
    return _client


def _str(value) -> str:
    """Coerce a JSON value to a stripped string (models sometimes return null or numbers)."""
    if value is None:
        return ""
    return str(value).strip()


def _parse_json_text(text: str):
    """Parse a JSON reply, tolerating a surrounding markdown code fence."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _ask_json(prompt: str, model: str, max_tokens: int, call_type: str):
    """
    Send a single-prompt request and return the parsed JSON reply, or None on failure.

    Responses may begin with thinking blocks, so the reply is read from the text blocks.
    """
    response = _get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        output_config={"effort": AI_EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )
    usage_tracker.record(response, call_type, model=model)

    if response.stop_reason == "refusal":
        log.warning("%s: model declined the request (%s)", call_type, response.stop_details)
        return None
    if response.stop_reason == "max_tokens":
        log.warning("%s: reply hit max_tokens=%d and may be truncated", call_type, max_tokens)

    text = "".join(b.text for b in response.content if b.type == "text")
    log.debug("%s response length: %d chars", call_type, len(text))
    try:
        return _parse_json_text(text)
    except json.JSONDecodeError as e:
        log.error("%s: failed to parse JSON reply: %s\nResponse: %s", call_type, e, text[:500])
        return None


def extract_companies_from_html(html: str, sni_code: str) -> list[dict]:
    """
    Use Sonnet to extract structured company data from an Allabolag.se HTML page.

    Returns a list of dicts with keys:
        name, org_number, status, revenue, employees, location, allabolag_url
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "path"]):
        tag.decompose()
    clean_html = str(soup.body) if soup.body else str(soup)

    if len(clean_html) > 100_000:
        clean_html = clean_html[:100_000]

    log.debug("Sending %d chars of cleaned HTML to Sonnet for extraction", len(clean_html))

    companies = _ask_json(
        model=ANTHROPIC_MODEL,
        max_tokens=AI_MAX_TOKENS,
        call_type="extract_companies",
        prompt=(
            "You are a data extraction assistant. Extract ALL companies listed on this "
            "Allabolag.se search results page.\n\n"
            "For each company, extract:\n"
            "- name: Company name\n"
            "- org_number: Swedish org number (10 digits, no dash)\n"
            "- status: Active/inactive status if shown\n"
            "- revenue: Revenue/omsättning if shown\n"
            "- employees: Number of employees if shown\n"
            "- location: City/municipality if shown\n"
            "- allabolag_url: The href link to the company's Allabolag page\n\n"
            "Return ONLY a JSON array of objects. No markdown, no explanation.\n"
            "If no companies are found, return an empty array: []\n\n"
            f"SNI code context: {sni_code}\n\n"
            f"HTML:\n{clean_html}"
        ),
    )

    if not isinstance(companies, list):
        log.error("Company extraction returned non-list: %s", type(companies))
        return []

    log.info("Sonnet extracted %d companies from HTML", len(companies))

    result = []
    for c in companies:
        if not isinstance(c, dict):
            continue
        # Normalize every field to a string so callers can .strip() safely
        c = {k: _str(v) for k, v in c.items()}
        c["org_number"] = c.get("org_number", "").replace("-", "").replace(" ", "")
        result.append(c)

    return result


def enrich_lead_from_search(
    company_name: str,
    org_number: str,
    location: str,
    search_results: list[dict],
) -> dict:
    """
    Use AI to extract contact info from search results.
    Returns dict with keys: website, linkedin, description, contacts (list of dicts).
    """
    if not search_results:
        return {}

    formatted = []
    for i, r in enumerate(search_results, 1):
        formatted.append(
            f"[{i}] {r.get('title', '')}\n"
            f"    URL: {r.get('href', '')}\n"
            f"    {r.get('body', '')}"
        )
    results_text = "\n\n".join(formatted)

    log.debug("Sending %d search results to Haiku for enrichment of '%s'",
              len(search_results), company_name)

    data = _ask_json(
        model=ANTHROPIC_MODEL_LIGHT,
        max_tokens=AI_SEARCH_MAX_TOKENS,
        call_type="enrich_from_search",
        prompt=(
            "You are a lead generation assistant. From the search results below, "
            "extract contact information for this Swedish company.\n\n"
            f"Company: {company_name}\n"
            f"Org number: {org_number}\n"
            f"Location: {location}\n\n"
            "Extract and return as JSON (no markdown, no explanation):\n"
            "{\n"
            '  "website": "company\'s own website URL (not social media or directories)",\n'
            '  "linkedin": "LinkedIn company or profile URL",\n'
            '  "description": "1-2 sentence summary of what the company does",\n'
            '  "contacts": [\n'
            "    {\n"
            '      "name": "full name of the person",\n'
            '      "title": "exact job title as written (e.g. VD, CEO, CFO, ekonomichef, Säljchef)",\n'
            '      "email": "their personal/direct email (NOT info@ or kontakt@)",\n'
            '      "phone": "their direct phone number (NOT switchboard/växel)",\n'
            '      "phone_type": "mobile|direct|switchboard",\n'
            '      "email_type": "personal|role|generic",\n'
            '      "linkedin": "their personal LinkedIn URL if found"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- Only include data you're confident belongs to THIS company\n"
            "- For website, return ONLY the company's own domain — NEVER directories like "
            "allabolag.se, bolagsfakta.se, allbiz.se, hitta.se, eniro.se, proff.se, "
            "foretagsfakta.se, ratsit.se, merinfo.se, upplysning.se, largestcompanies.se, "
            "europages.com, kompass.com. Return empty string if only directories found.\n"
            "- List ALL executive contacts you can find — VD/CEO, CFO/ekonomichef, CTO, Säljchef, etc.\n"
            "- For phone, ONLY include direct numbers for the named person — NEVER switchboard/växel\n"
            "- Swedish mobile numbers start with 07 (070, 072, 073, 076, 079)\n"
            "- phone_type: 'mobile' for 07x numbers, 'direct' for named person's line, 'switchboard' for växel/reception\n"
            "- email_type: 'personal' for firstname.lastname@, 'role' for vd@/ceo@, 'generic' for info@/kontakt@\n"
            "- Leave fields as empty string if not found\n"
            "- contacts should be an empty array [] if no named people found\n\n"
            f"Search results:\n{results_text}"
        ),
    )

    if not isinstance(data, dict):
        log.error("Enrichment returned non-dict: %s", type(data))
        return {}

    # Convert raw contact dicts to Contact objects
    contacts = _parse_contacts(data.get("contacts", []), source="search")

    result = {
        "website": _str(data.get("website")),
        "linkedin": _str(data.get("linkedin")),
        "description": _str(data.get("description")),
        "contacts": contacts,
    }

    log.info("Search enrichment for '%s': website=%s, linkedin=%s, %d contacts",
             company_name, bool(result["website"]), bool(result["linkedin"]), len(contacts))

    return result


def _parse_contacts(raw_contacts: list, source: str = "") -> list[Contact]:
    """Convert a list of raw contact dicts from AI into Contact objects."""
    contacts = []
    if not isinstance(raw_contacts, list):
        return contacts

    for raw in raw_contacts:
        if not isinstance(raw, dict):
            continue
        name = _str(raw.get("name"))
        if not name:
            continue
        contacts.append(Contact(
            name=name,
            title=_str(raw.get("title")),
            email=_str(raw.get("email")),
            phone=_str(raw.get("phone")),
            phone_type=_str(raw.get("phone_type")),
            email_type=_str(raw.get("email_type")),
            linkedin=_str(raw.get("linkedin")),
            confidence=0.0,  # will be computed by enricher
            source=source,
        ))

    return contacts


def _clean_html(html: str) -> str:
    """Strip scripts/styles/nav and return text-heavy HTML."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "path", "img", "video", "iframe"]):
        tag.decompose()
    for tag in soup(["nav", "header", "footer"]):
        tag.decompose()
    body = soup.body if soup.body else soup
    return body.get_text("\n", strip=True)


# Regex patterns for pre-filtering (same as enricher.py)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+46|0)[\s\-]?\d{1,3}[\s\-]?\d{2,3}[\s\-]?\d{2}[\s\-]?\d{2}")


def _pre_filter_website_text(text: str, context_chars: int = 500) -> str:
    """
    Keep only contact-relevant sections of scraped website text.
    Sections with emails/phones get a generous context window.
    Sections without get truncated to a short summary for description context.
    """
    PAGE_SEP = "=== PAGE:"
    sections = re.split(r"(=== PAGE:.*?===\n)", text)

    filtered_parts = []
    for i, section in enumerate(sections):
        # Keep page headers as-is
        if section.startswith(PAGE_SEP):
            filtered_parts.append(section)
            continue

        if not section.strip():
            continue

        # Find all email/phone match positions
        matches = list(_EMAIL_RE.finditer(section)) + list(_PHONE_RE.finditer(section))

        if matches:
            # Keep context_chars around each match, merge overlapping windows
            windows = []
            for m in sorted(matches, key=lambda x: x.start()):
                start = max(0, m.start() - context_chars)
                end = min(len(section), m.end() + context_chars)
                if windows and start <= windows[-1][1]:
                    windows[-1] = (windows[-1][0], end)
                else:
                    windows.append((start, end))

            kept = []
            for start, end in windows:
                kept.append(section[start:end])
            filtered_parts.append("\n...\n".join(kept))
        else:
            # No contacts found — keep just a short summary for description
            filtered_parts.append(section[:200])

    result = "".join(filtered_parts)
    log.debug("Pre-filtered website text: %d -> %d chars", len(text), len(result))
    return result


def discover_team_pages(homepage_html: str, base_url: str) -> list[str]:
    """
    Identify URLs on the homepage that likely lead to team/leadership pages
    using keyword matching (no AI call needed).
    Returns a list of full URLs to fetch.
    """
    TEAM_KEYWORDS = {
        "team", "ledning", "medarbetare", "om-oss", "om oss", "about",
        "kontakt", "contact", "organisation", "styrelse", "personal",
        "management", "leadership", "staff", "meet-the-team", "who-we-are",
        "our-people", "about-us", "anställda", "employees", "vårt-team",
    }

    soup = BeautifulSoup(homepage_html, "lxml")
    scored_links: list[tuple[str, int]] = []
    seen_hrefs = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        # Normalize relative URLs
        if href.startswith("/"):
            full_url = urljoin(base_url, href)
        elif href.startswith("http"):
            full_url = href
        else:
            full_url = urljoin(base_url + "/", href)

        # Only include same-domain links
        if urlparse(full_url).netloc != urlparse(base_url).netloc:
            continue
        if full_url in seen_hrefs:
            continue
        seen_hrefs.add(full_url)

        # Score by keyword matches in URL path and link text
        path = urlparse(full_url).path.lower()
        link_text = text.lower()
        score = sum(1 for kw in TEAM_KEYWORDS if kw in path or kw in link_text)

        if score > 0:
            scored_links.append((full_url, score))

    if not scored_links:
        log.debug("No team/leadership links found on homepage for %s", base_url)
        return []

    # Sort by score descending, take top N
    scored_links.sort(key=lambda x: x[1], reverse=True)
    pages = [url for url, _ in scored_links[:MAX_WEBSITE_PAGES]]

    log.info("Heuristic discovered %d team/leadership pages on %s", len(pages), base_url)
    return pages


def scrape_website_pages(website_url: str) -> str:
    """
    Fetch the homepage + team/contact pages from a company website.
    Uses AI-powered page discovery when enabled, falls back to hardcoded paths.
    Returns combined text content from all successfully fetched pages.
    """
    parsed = urlparse(website_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    }

    all_text = []
    fetched_urls = set()

    with httpx.Client(
        timeout=WEBSITE_SCRAPE_TIMEOUT,
        follow_redirects=True,
        verify=False,
    ) as client:

        def _fetch_page(url: str, label: str) -> str:
            """Fetch a single page and add its text to all_text. Returns the raw HTML if successful."""
            if url in fetched_urls:
                return ""
            fetched_urls.add(url)
            try:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
                    text = _clean_html(resp.text)
                    if text and len(text) > 50:
                        all_text.append(f"=== PAGE: {label} ({url}) ===\n{text}")
                        log.debug("Fetched %s — %d chars of text", url, len(text))
                        return resp.text
                    else:
                        log.debug("Skipped %s — too little text (%d chars)", url, len(text))
                else:
                    log.debug("Skipped %s — status %d", url, resp.status_code)
            except Exception as e:
                log.debug("Failed to fetch %s: %s", url, e)
            return ""

        # Always fetch homepage first
        homepage_url = urljoin(base, "/")
        homepage_html = _fetch_page(homepage_url, "homepage")

        # Discover team/contact pages via keyword heuristic
        pages_to_fetch = []
        if homepage_html:
            pages_to_fetch = discover_team_pages(homepage_html, base)
            log.info("Heuristic discovery found %d pages for %s", len(pages_to_fetch), base)

        # If discovery returned nothing, fall back to hardcoded paths
        if not pages_to_fetch:
            log.debug("Using hardcoded paths for %s", base)
            pages_to_fetch = [urljoin(base, p) for p in WEBSITE_SCRAPE_PATHS if p != "/"]

        # Fetch discovered/hardcoded pages
        for url in pages_to_fetch[:MAX_WEBSITE_PAGES]:
            label = urlparse(url).path or url
            _fetch_page(url, label)

    combined = "\n\n".join(all_text)
    log.info("Scraped %d pages from %s — total %d chars", len(all_text), base, len(combined))
    return combined


def extract_contacts_from_website(
    company_name: str,
    website_text: str,
) -> dict:
    """
    Use AI to extract contacts from website text.
    Returns dict with keys: contacts (list[Contact]), description, all_emails, all_phones.
    """
    if not website_text:
        return {}

    # Pre-filter to keep only contact-relevant sections, then hard-cap
    website_text = _pre_filter_website_text(website_text)
    if len(website_text) > 20_000:
        website_text = website_text[:20_000]

    log.debug("Sending %d chars of website text to AI for '%s'", len(website_text), company_name)

    data = _ask_json(
        model=ANTHROPIC_MODEL_LIGHT,
        max_tokens=AI_WEBSITE_MAX_TOKENS,
        call_type="extract_website_contacts",
        prompt=(
            "You are extracting executive contact details from a Swedish company's website.\n\n"
            f"Company: {company_name}\n\n"
            "Extract ALL named people with their contact details:\n"
            "{\n"
            '  "contacts": [\n'
            "    {\n"
            '      "name": "full name",\n'
            '      "title": "exact job title as written on the site (e.g. VD, CEO, Ekonomichef, Säljchef)",\n'
            '      "email": "their personal/direct email address",\n'
            '      "phone": "their direct phone number",\n'
            '      "phone_type": "mobile|direct|switchboard",\n'
            '      "email_type": "personal|role|generic",\n'
            '      "linkedin": "their personal LinkedIn URL if found"\n'
            "    }\n"
            "  ],\n"
            '  "all_emails": ["every email address found on the site"],\n'
            '  "all_phones": ["every phone number found on the site"],\n'
            '  "description": "1-2 sentence summary of what the company does"\n'
            "}\n\n"
            "Rules:\n"
            "- List EVERY named person you can find — executives, managers, team leads, etc.\n"
            "- For each person, include their DIRECT phone and PERSONAL email — NOT the switchboard/växel\n"
            "- Swedish mobile numbers start with 07 (070, 072, 073, 076, 079) — these are always direct\n"
            "- Numbers described as 'växel', 'reception', or 'kundtjänst' are switchboard — do NOT assign to a person\n"
            "- phone_type: 'mobile' for 07x numbers, 'direct' for person's named line, 'switchboard' for växel\n"
            "- email_type: 'personal' for firstname.lastname@ pattern, 'role' for vd@/ceo@/sales@, 'generic' for info@/kontakt@\n"
            "- title must be EXACTLY as written on the site, not translated or paraphrased\n"
            "- Include all_emails and all_phones for reference (including generic ones)\n"
            "- Exclude noreply@ and support-ticket emails\n"
            "- Return ONLY valid JSON, no markdown or explanation\n"
            "- If nothing found for a field, use empty string or empty array\n\n"
            f"Website text:\n{website_text}"
        ),
    )

    if not isinstance(data, dict):
        log.error("Website extraction returned non-dict: %s", type(data))
        return {}

    contacts = _parse_contacts(data.get("contacts", []), source="website")

    result = {
        "contacts": contacts,
        "description": _str(data.get("description")),
        "all_emails": [_str(e) for e in data.get("all_emails") or [] if e],
        "all_phones": [_str(p) for p in data.get("all_phones") or [] if p],
    }

    log.info("Website extraction for '%s': %d contacts, %d emails, %d phones",
             company_name, len(contacts),
             len(result["all_emails"]), len(result["all_phones"]))
    for c in contacts:
        log.info("  Contact: %s (%s) — phone: %s [%s], email: %s [%s]",
                 c.name, c.title, c.phone, c.phone_type, c.email, c.email_type)

    return result


def infer_personal_email(name: str, domain: str) -> list[str]:
    """
    Generate likely personal email addresses from a person's name and company domain.
    Returns a list of candidates (most likely first).
    """
    if not name or not domain:
        return []

    # Normalize name: "Anna-Karin Svensson" → parts
    parts = name.lower().strip().split()
    if len(parts) < 2:
        return []

    # Handle Swedish characters in email
    char_map = str.maketrans("åäöéü", "aaoeu")
    first = parts[0].translate(char_map).replace("-", "")
    last = parts[-1].translate(char_map).replace("-", "")
    initial = first[0] if first else ""

    candidates = [
        f"{first}.{last}@{domain}",
        f"{first}@{domain}",
        f"{initial}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{last}.{first}@{domain}",
    ]

    return candidates
