"""Configuration for the LeadGen tool."""

ALLABOLAG_BASE_URL = "https://www.allabolag.se"

# Browser settings
HEADLESS = True
REQUEST_DELAY = (2, 5)  # Random delay range in seconds between requests
PAGE_LOAD_TIMEOUT = 30_000  # ms

# Search settings
MAX_SEARCH_RESULTS_PER_COMPANY = 5
ENRICHMENT_DELAY = (1, 3)  # Delay between enrichment searches

# Export
DEFAULT_OUTPUT_FORMAT = "csv"
DEFAULT_OUTPUT_FILE = "leads.csv"

# AI settings
AI_ENABLED = True
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_MODEL_LIGHT = "claude-haiku-4-5-20251001"  # cheaper model for simpler extraction tasks
AI_MAX_TOKENS = 4096
AI_WEBSITE_MAX_TOKENS = 2048  # for extract_contacts_from_website (richer output)

# Website scraping
WEBSITE_SCRAPE_TIMEOUT = 15  # seconds
WEBSITE_SCRAPE_PATHS = ["/", "/kontakt", "/contact", "/om-oss", "/about",
                        "/medarbetare", "/team", "/about-us", "/ledning", "/organisation"]
MAX_WEBSITE_PAGES = 3           # max pages to fetch per company website

# Multi-contact export
MAX_CONTACTS_COLUMNS = 5  # max contacts per row in "columns" export mode

# Skip / timeout
ENRICHMENT_TIMEOUT = 60          # max seconds per company enrichment before skipping
SKIP_IF_NO_WEBSITE = True        # skip deep enrichment if no real company website found
SKIP_WEBSITE_IF_GOOD_SEARCH = True  # skip website scrape if search already found personal email + mobile

# Cache
CACHE_ENABLED = True
CACHE_TTL_DAYS = 30              # days before cached enrichment results expire
