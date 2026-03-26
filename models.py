"""Data models for scraped companies and enriched leads."""

from dataclasses import dataclass, field, asdict


@dataclass
class Company:
    """A company scraped from Allabolag.se."""
    name: str
    org_number: str
    sni_code: str = ""
    status: str = ""
    revenue: str = ""
    employees: str = ""
    location: str = ""
    address: str = ""
    allabolag_url: str = ""


@dataclass
class Contact:
    """A person found during enrichment."""
    name: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    phone_type: str = ""    # "mobile", "direct", "switchboard", "unknown"
    email_type: str = ""    # "personal", "role", "generic"
    linkedin: str = ""
    confidence: float = 0.0  # 0.0 - 1.0
    source: str = ""         # "website", "search", "inferred"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Lead(Company):
    """A company enriched with contact information from web searches."""
    website: str = ""
    linkedin: str = ""
    description: str = ""
    contacts: list[Contact] = field(default_factory=list)
    confidence: float = 0.0  # overall lead confidence (max of contact confidences)

    # Legacy flat fields — populated from best contact for backward compat
    email: str = ""
    phone: str = ""
    contact_person: str = ""
    contact_title: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        # Serialize contacts as list of dicts
        d["contacts"] = [c.to_dict() if isinstance(c, Contact) else c for c in self.contacts]
        return d

    def set_best_contact(self):
        """Populate legacy flat fields from the highest-confidence contact."""
        if not self.contacts:
            return
        best = max(self.contacts, key=lambda c: c.confidence)
        self.email = best.email or self.email
        self.phone = best.phone or self.phone
        self.contact_person = best.name or self.contact_person
        self.contact_title = best.title or self.contact_title
        self.confidence = best.confidence

    @classmethod
    def from_company(cls, company: Company) -> "Lead":
        return cls(**{k: v for k, v in asdict(company).items()})
