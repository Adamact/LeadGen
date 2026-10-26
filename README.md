# LeadGen

Scrape Swedish companies from Allabolag.se by SNI code and enrich them with contact information via web search and AI-powered extraction.

## How It Works

1. **Scrape** — Queries Allabolag.se's segmentering page by SNI code, splitting employee ranges into small windows to bypass the 10-result paywall limit
2. **Enrich** — Searches DuckDuckGo for each company, finds their website, scrapes team/contact pages, and uses Claude AI to extract executive contacts
3. **Export** — Writes leads to CSV or JSON with multiple contacts per company, confidence scores, and phone/email classification

## Setup

Requires Python 3.10+.

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Set your Anthropic API key for AI features:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```bash
python main.py <sni_code> [options]
```

### Examples

```bash
# Software companies (SNI 62100), default settings
python main.py 62100

# Limit to 3 pages, custom output file
python main.py 62100 --pages 3 --output leads.csv

# Export as JSON, skip enrichment
python main.py 70200 --format json --no-enrich

# Fetch detail pages from Allabolag for extra data
python main.py 62100 --details

# One row per company instead of one row per contact
python main.py 62100 --contact-format columns

# Only include high-confidence leads
python main.py 62100 --min-confidence 0.5

# Run without AI (regex fallback only, no API key needed)
python main.py 62100 --no-ai

# Show browser window for debugging
python main.py 62100 --no-headless

# Custom employee and revenue filters
python main.py 62100 --employees-from 10 --employees-to 50 --revenue-from 5000

# Bypass cache to re-fetch everything
python main.py 62100 --no-cache
```

### All Options

| Flag | Default | Description |
|------|---------|-------------|
| `sni_code` | *(required)* | SNI code to search (e.g. `62100` for software development) |
| `--pages`, `-p` | `5` | Max result pages to scrape |
| `--output`, `-o` | `leads.csv` | Output file path |
| `--format`, `-f` | `csv` | Output format: `csv` or `json` |
| `--employees-from` | `20` | Minimum number of employees |
| `--employees-to` | `150` | Maximum number of employees |
| `--revenue-from` | `100` | Minimum revenue (thousand SEK) |
| `--revenue-to` | `302609000` | Maximum revenue (thousand SEK) |
| `--profit-from` | `100` | Minimum operating profit (thousand SEK) |
| `--profit-to` | `85733000` | Maximum operating profit (thousand SEK) |
| `--no-enrich` | — | Skip web enrichment (scrape Allabolag only) |
| `--details`, `-d` | — | Fetch each company's Allabolag detail page (slower) |
| `--no-ai` | — | Disable AI extraction (regex fallback, no API key needed) |
| `--no-headless` | — | Show browser window (useful for debugging) |
| `--contact-format` | `rows` | CSV layout: `rows` (one row per contact) or `columns` (one row per company) |
| `--min-confidence` | `0.0` | Minimum confidence score to include a contact (0.0–1.0) |
| `--max-contacts` | `5` | Max contacts per company in `columns` mode |
| `--no-cache` | — | Bypass enrichment cache (re-fetch everything) |
| `--enrichment-timeout` | `60` | Max seconds per company enrichment before skipping |

## Output

### CSV (rows mode — default)

One row per contact. Company fields are repeated for each contact:

```
name, org_number, sni_code, status, location, address, revenue, employees,
website, linkedin, contact_name, contact_title, contact_email, contact_phone,
contact_phone_type, contact_email_type, contact_confidence, contact_linkedin,
contact_source, lead_confidence, description, allabolag_url
```

### CSV (columns mode)

One row per company. Contacts are numbered columns (`contact_1_name`, `contact_1_email`, `contact_2_name`, etc.).

### JSON

Nested structure with a `contacts` array per lead.

## Incremental Runs

Re-running with the same output file automatically skips companies already present (matched by `org_number`) and appends only new leads.

Enrichment results are cached locally in `.leadgen_cache/` for 30 days. Use `--no-cache` to force fresh lookups.

## Cost Optimization

AI calls use a tiered model strategy to minimize API costs:

- **Sonnet** (`claude-sonnet-5-5`) for complex HTML parsing (company extraction from Allabolag)
- **Haiku** (`claude-haiku-5-5`) for structured extraction (search enrichment, website contacts)
- **Keyword heuristic** for team page discovery (no API call)
- **Smart skipping** — website scrape is skipped when search already yields a quality contact (personal email + mobile)
- **Pre-filtering** — website text is trimmed to contact-relevant sections before sending to the API

Requests run at low effort (`AI_EFFORT` in `config.py`). Each run ends with a summary of API calls, tokens and estimated cost.

## Development

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

CI runs both on every push and pull request.

## Logs

Debug logs are written to `logs/leadgen_<timestamp>.log` on each run.

## Disclaimer

This tool is provided for **educational and research purposes only**. By using it, you acknowledge and agree to the following:

- **Terms of Service** — Automated scraping may violate Allabolag.se's terms of service. It is your responsibility to review and comply with the terms of any website you scrape.
- **GDPR** — This tool collects personal data (names, email addresses, phone numbers) of individuals at Swedish companies. Under the EU General Data Protection Regulation (GDPR), you must have a **lawful basis** (e.g. legitimate interest) for processing this data. You are solely responsible for ensuring your use complies with GDPR and any other applicable data protection laws.
- **No spam** — The output of this tool is not intended for unsolicited mass outreach, spam, or any form of harassment.
- **No warranty** — This software is provided "as is" without warranty of any kind. The authors are not liable for any legal consequences arising from its use.

Use responsibly and at your own risk.

## Project Structure

```
main.py          CLI entry point and pipeline orchestration
scraper.py       Allabolag.se scraper (Playwright + BeautifulSoup)
enricher.py      Web search enrichment and contact extraction
ai.py            Claude AI integration (extraction, email inference)
export.py        CSV/JSON export with multi-contact support
models.py        Data models (Company, Contact, Lead)
config.py        Configuration constants
cache.py         File-based enrichment cache
logger.py        Logging setup
tests/           Unit tests (no network or API calls)
```
