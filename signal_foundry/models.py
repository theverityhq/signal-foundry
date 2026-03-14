from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Business:
    name: str
    category: str
    website: str
    phone: str = ""
    address: str = ""
    city: str = ""


@dataclass
class SiteSignals:
    has_hours: bool = False
    has_phone: bool = False
    has_address: bool = False
    has_menu_like_content: bool = False
    has_service_like_content: bool = False
    has_product_like_content: bool = False


@dataclass
class AuditResult:
    business_name: str
    category: str
    website: str
    phone: str
    address: str
    city: str
    prospect_fit: str
    prospect_fit_score: int
    status: str
    score: int
    schema_types_found: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    opportunity_summary: str = ""
    recommended_type: str = ""
    recommended_jsonld: str = ""
    current_jsonld: str = ""
    schema_gap_summary: str = ""
    recommendation_reasons: list[str] = field(default_factory=list)
    pages_scanned: list[str] = field(default_factory=list)
    fetch_failures: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
