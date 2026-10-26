import config
import enricher
from enricher import (
    _is_personal_email,
    _try_infer_emails,
    classify_phone,
    compute_confidence,
    merge_contacts,
)
from models import Company, Contact


def test_classify_phone():
    assert classify_phone("070-123 45 67") == "mobile"
    assert classify_phone("+46 73 123 45 67") == "mobile"
    assert classify_phone("08-123 456 78") == "landline"
    assert classify_phone("12345") == "unknown"


def test_is_personal_email():
    assert _is_personal_email("anna.svensson@acme.se")
    assert not _is_personal_email("info@acme.se")
    assert not _is_personal_email("vd@acme.se")


def test_compute_confidence_rewards_personal_details():
    weak = Contact(name="Anna Svensson", email="info@acme.se", email_type="generic")
    strong = Contact(
        name="Anna Svensson", title="VD", email="anna.svensson@acme.se",
        phone="070-123 45 67", source="website",
    )
    assert compute_confidence(strong) > compute_confidence(weak)
    assert compute_confidence(strong) <= 1.0


def test_merge_contacts_prefers_website_and_fills_blanks():
    merged = merge_contacts([
        Contact(name="Anna Svensson", phone="070-123 45 67", source="search"),
        Contact(name="anna svensson", title="VD", email="anna.svensson@acme.se", source="website"),
        Contact(name="Erik Berg", source="search"),
    ])
    assert len(merged) == 2
    anna = merged[0]
    assert anna.source == "website"
    assert anna.title == "VD"
    assert anna.email == "anna.svensson@acme.se"
    assert anna.phone == "070-123 45 67"


def test_infer_emails_fills_missing_only():
    contacts = [
        Contact(name="Anna Svensson"),
        Contact(name="Erik Berg", email="info@acme.se", email_type="generic"),
    ]
    _try_infer_emails(contacts, ["info@acme.se"])
    assert contacts[0].email == "anna.svensson@acme.se"
    assert contacts[0].email_type == "personal"
    # A real (generic) email must not be relabelled as personal
    assert contacts[1].email == "info@acme.se"
    assert contacts[1].email_type == "generic"


def test_infer_emails_skips_freemail_domains():
    contacts = [Contact(name="Anna Svensson")]
    _try_infer_emails(contacts, ["someone@gmail.com"])
    assert contacts[0].email == ""


def test_cli_toggles_are_read_at_runtime(monkeypatch):
    """main.py sets config.* after import; enricher must see the new values."""
    monkeypatch.setattr(config, "AI_ENABLED", False)
    monkeypatch.setattr(config, "CACHE_ENABLED", False)
    monkeypatch.setattr(enricher, "_gather_search_results", lambda c: [
        {"title": "Acme AB", "href": "https://acme.se/", "body": "Ring 070-123 45 67"},
    ])
    called = []
    monkeypatch.setattr(enricher, "_enrich_with_ai", lambda *a: called.append(a) or {})

    info = enricher._search_and_enrich(Company(name="Acme AB", org_number="5560000000"))

    assert not called
    assert info["website"] == "https://acme.se/"
    assert info["contacts"][0].phone == "070-123 45 67"
