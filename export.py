"""Export leads to CSV or JSON with multi-contact support."""

import csv
import json
from pathlib import Path

from config import MAX_CONTACTS_COLUMNS
from models import Lead, Contact
from logger import log


# Company-level fields (shared across all contact rows)
COMPANY_FIELDS = [
    "name", "org_number", "sni_code", "status", "location", "address",
    "revenue", "employees", "website", "linkedin",
]

# Contact-level fields for rows mode
CONTACT_FIELDS = [
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "contact_phone_type", "contact_email_type", "contact_confidence",
    "contact_linkedin", "contact_source",
]

# Trailing fields
TAIL_FIELDS = ["lead_confidence", "description", "allabolag_url"]


def _contact_row(contact: Contact) -> dict:
    """Convert a Contact to a flat dict with prefixed keys."""
    return {
        "contact_name": contact.name,
        "contact_title": contact.title,
        "contact_email": contact.email,
        "contact_phone": contact.phone,
        "contact_phone_type": contact.phone_type,
        "contact_email_type": contact.email_type,
        "contact_confidence": f"{contact.confidence:.2f}",
        "contact_linkedin": contact.linkedin,
        "contact_source": contact.source,
    }


def _lead_base(lead: Lead) -> dict:
    """Extract company-level + tail fields from a Lead."""
    return {
        "name": lead.name,
        "org_number": lead.org_number,
        "sni_code": lead.sni_code,
        "status": lead.status,
        "location": lead.location,
        "address": lead.address,
        "revenue": lead.revenue,
        "employees": lead.employees,
        "website": lead.website,
        "linkedin": lead.linkedin,
        "lead_confidence": f"{lead.confidence:.2f}",
        "description": lead.description,
        "allabolag_url": lead.allabolag_url,
    }


def _filter_contacts(leads: list[Lead], min_confidence: float) -> list[Lead]:
    """Filter out contacts below the minimum confidence threshold."""
    if min_confidence <= 0:
        return leads
    for lead in leads:
        lead.contacts = [c for c in lead.contacts if c.confidence >= min_confidence]
        lead.set_best_contact()
    return leads


def to_csv_rows(leads: list[Lead], filepath: str, min_confidence: float = 0.0,
                append: bool = False) -> str:
    """Export leads to CSV — one row per contact (company fields repeated)."""
    leads = _filter_contacts(leads, min_confidence)
    path = Path(filepath)
    fieldnames = COMPANY_FIELDS + CONTACT_FIELDS + TAIL_FIELDS
    mode = "a" if append and path.exists() else "w"

    with path.open(mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()

        for lead in leads:
            base = _lead_base(lead)
            if lead.contacts:
                for contact in lead.contacts:
                    row = {**base, **_contact_row(contact)}
                    writer.writerow(row)
            else:
                row = {**base, **{k: "" for k in CONTACT_FIELDS}}
                writer.writerow(row)

    return str(path.resolve())


def to_csv_columns(leads: list[Lead], filepath: str, min_confidence: float = 0.0,
                    max_contacts: int = MAX_CONTACTS_COLUMNS, append: bool = False) -> str:
    """Export leads to CSV — one row per company, contacts as numbered columns."""
    leads = _filter_contacts(leads, min_confidence)
    path = Path(filepath)

    contact_col_fields = []
    for i in range(1, max_contacts + 1):
        for suffix in ["name", "title", "email", "phone", "phone_type", "confidence"]:
            contact_col_fields.append(f"contact_{i}_{suffix}")

    fieldnames = COMPANY_FIELDS + contact_col_fields + TAIL_FIELDS
    mode = "a" if append and path.exists() else "w"

    with path.open(mode, newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()

        for lead in leads:
            row = _lead_base(lead)
            sorted_contacts = sorted(lead.contacts, key=lambda c: c.confidence, reverse=True)
            for i, contact in enumerate(sorted_contacts[:max_contacts], 1):
                row[f"contact_{i}_name"] = contact.name
                row[f"contact_{i}_title"] = contact.title
                row[f"contact_{i}_email"] = contact.email
                row[f"contact_{i}_phone"] = contact.phone
                row[f"contact_{i}_phone_type"] = contact.phone_type
                row[f"contact_{i}_confidence"] = f"{contact.confidence:.2f}"
            writer.writerow(row)

    return str(path.resolve())


def to_json(leads: list[Lead], filepath: str, min_confidence: float = 0.0,
            append: bool = False) -> str:
    """Export leads to a JSON file with nested contacts."""
    leads = _filter_contacts(leads, min_confidence)
    path = Path(filepath)

    existing = []
    if append and path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    data = existing + [lead.to_dict() for lead in leads]
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path.resolve())


def load_existing_org_numbers(filepath: str, fmt: str = "csv") -> set[str]:
    """Load org_number values from an existing export file. Returns a set of org numbers."""
    path = Path(filepath)
    if not path.exists():
        return set()

    org_numbers = set()
    try:
        if fmt == "json":
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                org = item.get("org_number", "")
                if org:
                    org_numbers.add(org)
        else:
            with path.open("r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    org = row.get("org_number", "")
                    if org:
                        org_numbers.add(org)
    except Exception as e:
        log.warning("Could not read existing file %s: %s", filepath, e)
        return set()

    log.info("Loaded %d existing org numbers from %s", len(org_numbers), filepath)
    return org_numbers


def export_leads(leads: list[Lead], filepath: str, fmt: str = "csv",
                 contact_format: str = "rows", min_confidence: float = 0.0,
                 max_contacts: int = MAX_CONTACTS_COLUMNS,
                 append: bool = False) -> str:
    """Export leads to the specified format."""
    if fmt == "json":
        return to_json(leads, filepath, min_confidence, append)
    if contact_format == "columns":
        return to_csv_columns(leads, filepath, min_confidence, max_contacts, append)
    return to_csv_rows(leads, filepath, min_confidence, append)
