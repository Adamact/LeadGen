import pytest

import config
import scraper
from export import export_leads, load_existing_org_numbers
from models import Contact, Lead


def test_employee_windows_cover_range_without_gaps():
    windows = scraper._build_employee_windows(20, 150)
    assert windows[0][0] == 20
    assert windows[-1][1] == 150
    for (_, end), (start, _) in zip(windows, windows[1:]):
        assert end == start


def test_regex_parser_used_when_ai_disabled(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    html = """
    <div><a href="/foretag/acme-ab/stockholm/-/5560000000">Acme AB</a> Anställda 25</div>
    <div><a href="/foretag/beta-ab/goteborg/-/5561111111">Beta AB</a></div>
    """
    companies = scraper._parse_company_list(html, "62010")
    assert [c.org_number for c in companies] == ["5560000000", "5561111111"]
    assert companies[0].location == "Stockholm"
    assert companies[0].employees == "25"


def _lead(org, *contacts):
    lead = Lead(name=f"Company {org}", org_number=org, contacts=list(contacts))
    lead.set_best_contact()
    return lead


def test_csv_rows_append_and_dedupe(tmp_path):
    out = tmp_path / "leads.csv"
    export_leads([_lead("1", Contact(name="A", confidence=0.9), Contact(name="B", confidence=0.2))],
                 str(out))
    export_leads([_lead("2")], str(out), append=True)

    assert load_existing_org_numbers(str(out)) == {"1", "2"}
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1 + 2 + 1  # header, two contacts for lead 1, one empty row for lead 2


def test_min_confidence_filters_contacts(tmp_path):
    out = tmp_path / "leads.json"
    export_leads([_lead("1", Contact(name="A", confidence=0.9), Contact(name="B", confidence=0.2))],
                 str(out), fmt="json", min_confidence=0.5)
    assert load_existing_org_numbers(str(out), "json") == {"1"}
    assert '"B"' not in out.read_text(encoding="utf-8")


CARD_HTML = """
<div class="results">
  <div class="SegmentationSearchResultCard-card"><div><h2><a href="/foretag/acme-ab/bor%C3%A5s/-/5591631279">Acme AB</a></h2>
    <span>Org.nr 559163-1279</span> <span>503 38 Borås</span> <span>Omsättning 2025</span> <span>33&nbsp;070</span>
    <span>Anställda</span> <span>20</span> <span>Ordförande Anna Svensson</span></div></div>
  <div class="SegmentationSearchResultCard-card"><div><h2><a href="/foretag/beta-ab/stockholm/-/5566467568">Beta AB</a></h2>
    <span>Omsättning 2026</span> <span>1 048 802</span> <span>Anställda</span> <span>25</span></div></div>
</div>
"""


def test_html_parser_reads_result_cards_without_ai(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    monkeypatch.setattr(scraper, "_parse_with_ai", lambda *a: pytest.fail("AI parser should not run"))
    companies = scraper._parse_company_list(CARD_HTML, "62100")
    assert [(c.name, c.location, c.employees, c.revenue) for c in companies] == [
        ("Acme AB", "Borås", "20", "33 070"),
        ("Beta AB", "Stockholm", "25", "1 048 802"),
    ]


def test_ai_parser_is_fallback_when_html_parser_finds_nothing(monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    called = []
    monkeypatch.setattr(scraper, "_parse_with_ai", lambda *a: called.append(a) or [])
    scraper._parse_company_list("<html><body>nothing here</body></html>", "62100")
    assert called
