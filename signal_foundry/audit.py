from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .models import AuditResult, Business, SiteSignals

LOGGER = logging.getLogger(__name__)

COMMON_PATHS = [
    "/",
    "/about",
    "/about-us",
    "/contact",
    "/contact-us",
    "/services",
    "/menu",
    "/products",
]

LOCAL_TYPES = {
    "LocalBusiness",
    "Restaurant",
    "Dentist",
    "AutoRepair",
    "BarberShop",
}

DETAIL_TYPES = {"Service", "Product", "Offer", "Menu", "MenuItem"}


def load_businesses(input_csv: Path) -> list[Business]:
    with input_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        businesses = []
        for row in reader:
            website = (row.get("website") or "").strip()
            businesses.append(
                Business(
                    name=(row.get("name") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    website=normalize_url(website),
                    phone=(row.get("phone") or "").strip(),
                    address=(row.get("address") or "").strip(),
                    city=(row.get("city") or "").strip(),
                )
            )
    return businesses


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return "https://" + url


def candidate_urls(base_url: str, max_pages: int) -> list[str]:
    if not base_url:
        return []
    base = base_url.rstrip("/") + "/"
    urls = [urljoin(base, path.lstrip("/")) for path in COMMON_PATHS]
    deduped = []
    seen = set()
    for url in urls:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
        if len(deduped) >= max_pages:
            break
    return deduped


def fetch_html(
    url: str,
    *,
    session: requests.Session,
    user_agent: str,
    timeout_seconds: int,
) -> str:
    response = session.get(
        url,
        headers={"User-Agent": user_agent},
        timeout=timeout_seconds,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text


def extract_jsonld_types(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    found: set[str] = set()
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text(strip=False)
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _walk_types(payload, found)
    return found


def _walk_types(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        schema_type = node.get("@type")
        if isinstance(schema_type, str):
            found.add(schema_type)
        elif isinstance(schema_type, list):
            for item in schema_type:
                if isinstance(item, str):
                    found.add(item)
        for value in node.values():
            _walk_types(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_types(item, found)


def detect_signals(html: str, business: Business) -> SiteSignals:
    lowered = html.lower()
    phone_digits = "".join(ch for ch in business.phone if ch.isdigit())
    return SiteSignals(
        has_hours=("hours" in lowered or "open" in lowered or "closed" in lowered),
        has_phone=(
            "tel:" in lowered
            or "phone" in lowered
            or (phone_digits and phone_digits[-7:] in "".join(ch for ch in lowered if ch.isdigit()))
        ),
        has_address=(
            "address" in lowered
            or "street" in lowered
            or "road" in lowered
            or (business.city and business.city.lower() in lowered)
        ),
        has_menu_like_content=("menu" in lowered),
        has_service_like_content=("services" in lowered or "service area" in lowered),
        has_product_like_content=("product" in lowered or "shop" in lowered or "$" in lowered),
    )


def merge_signals(existing: SiteSignals, incoming: SiteSignals) -> SiteSignals:
    return SiteSignals(
        has_hours=existing.has_hours or incoming.has_hours,
        has_phone=existing.has_phone or incoming.has_phone,
        has_address=existing.has_address or incoming.has_address,
        has_menu_like_content=existing.has_menu_like_content or incoming.has_menu_like_content,
        has_service_like_content=existing.has_service_like_content or incoming.has_service_like_content,
        has_product_like_content=existing.has_product_like_content or incoming.has_product_like_content,
    )


def score_business(schema_types: set[str], signals: SiteSignals) -> tuple[int, list[str], str]:
    score = 0
    missing: list[str] = []

    if schema_types:
        score += 25
    else:
        missing.append("structured_data")

    if any(schema_type in LOCAL_TYPES for schema_type in schema_types):
        score += 25
    else:
        missing.append("local_business_schema")

    if any(schema_type in DETAIL_TYPES for schema_type in schema_types):
        score += 20
    else:
        if signals.has_menu_like_content:
            missing.append("menu_schema")
        if signals.has_service_like_content:
            missing.append("service_schema")
        if signals.has_product_like_content:
            missing.append("product_schema")

    if signals.has_hours:
        score += 10
    else:
        missing.append("hours")

    if signals.has_phone:
        score += 10
    else:
        missing.append("phone")

    if signals.has_address:
        score += 10
    else:
        missing.append("address")

    if score >= 80:
        summary = "Strong foundation with smaller schema gaps."
    elif score >= 50:
        summary = "Moderate opportunity; some machine-readable signals exist but important gaps remain."
    else:
        summary = "High opportunity; weak machine-readable signals and likely poor AI/search understanding."

    return score, sorted(set(missing)), summary


def recommend_schema_type(business: Business, signals: SiteSignals) -> str:
    category = business.category.lower()
    if "restaurant" in category:
        return "Restaurant"
    if "dent" in category:
        return "Dentist"
    if "auto" in category or "mechanic" in category:
        return "AutoRepair"
    if "barber" in category:
        return "BarberShop"
    if signals.has_service_like_content:
        return "LocalBusiness + Service"
    return "LocalBusiness"


def recommend_jsonld(business: Business, schema_type: str) -> str:
    payload: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": schema_type.split(" + ")[0],
        "name": business.name,
        "url": business.website,
    }

    if business.phone:
        payload["telephone"] = business.phone
    if business.address:
        payload["address"] = business.address

    if schema_type == "Restaurant":
        payload["hasMenu"] = urljoin(business.website.rstrip("/") + "/", "menu")
    if schema_type == "LocalBusiness + Service":
        payload["makesOffer"] = {
            "@type": "Offer",
            "itemOffered": {
                "@type": "Service",
                "name": f"{business.category} services",
            },
        }

    return json.dumps(payload, indent=2)


def audit_businesses(
    businesses: list[Business],
    *,
    user_agent: str,
    timeout_seconds: int,
    max_pages_per_site: int,
) -> list[AuditResult]:
    session = requests.Session()
    results: list[AuditResult] = []

    for business in businesses:
        if not business.website:
            results.append(
                AuditResult(
                    business_name=business.name,
                    category=business.category,
                    website="",
                    city=business.city,
                    status="no_website",
                    score=0,
                    missing_fields=["website"],
                    opportunity_summary="No website found.",
                )
            )
            continue

        schema_types: set[str] = set()
        signals = SiteSignals()
        scanned_urls: list[str] = []
        notes: list[str] = []

        for url in candidate_urls(business.website, max_pages_per_site):
            try:
                html = fetch_html(
                    url,
                    session=session,
                    user_agent=user_agent,
                    timeout_seconds=timeout_seconds,
                )
            except requests.RequestException as exc:
                LOGGER.debug("Fetch failed for %s: %s", url, exc)
                continue

            scanned_urls.append(url)
            schema_types.update(extract_jsonld_types(html))
            signals = merge_signals(signals, detect_signals(html, business))

        if not scanned_urls:
            results.append(
                AuditResult(
                    business_name=business.name,
                    category=business.category,
                    website=business.website,
                    city=business.city,
                    status="unreachable",
                    score=0,
                    missing_fields=["crawlability"],
                    opportunity_summary="Could not fetch the site during the audit.",
                    notes=["No candidate pages returned a successful response."],
                )
            )
            continue

        score, missing_fields, summary = score_business(schema_types, signals)
        schema_type = recommend_schema_type(business, signals)
        notes.append(f"Scanned {len(scanned_urls)} page(s).")
        if not schema_types:
            notes.append("No JSON-LD detected on scanned pages.")

        results.append(
            AuditResult(
                business_name=business.name,
                category=business.category,
                website=business.website,
                city=business.city,
                status="ok",
                score=score,
                schema_types_found=sorted(schema_types),
                missing_fields=missing_fields,
                opportunity_summary=summary,
                recommended_type=schema_type,
                recommended_jsonld=recommend_jsonld(business, schema_type),
                pages_scanned=scanned_urls,
                notes=notes,
            )
        )

    return results


def write_reports(results: list[AuditResult], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_report(results, output_dir / "audit-report.json")
    write_csv_report(results, output_dir / "audit-report.csv")
    write_markdown_report(results, output_dir / "audit-report.md")


def write_json_report(results: list[AuditResult], path: Path) -> None:
    rows = [asdict(result) for result in results]
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def write_csv_report(results: list[AuditResult], path: Path) -> None:
    fieldnames = [
        "business_name",
        "category",
        "website",
        "city",
        "status",
        "score",
        "schema_types_found",
        "missing_fields",
        "opportunity_summary",
        "recommended_type",
        "pages_scanned",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "business_name": result.business_name,
                    "category": result.category,
                    "website": result.website,
                    "city": result.city,
                    "status": result.status,
                    "score": result.score,
                    "schema_types_found": ", ".join(result.schema_types_found),
                    "missing_fields": ", ".join(result.missing_fields),
                    "opportunity_summary": result.opportunity_summary,
                    "recommended_type": result.recommended_type,
                    "pages_scanned": ", ".join(result.pages_scanned),
                    "notes": " | ".join(result.notes),
                }
            )


def write_markdown_report(results: list[AuditResult], path: Path) -> None:
    lines = ["# Signal Foundry Audit Report", ""]
    for result in results:
        lines.append(f"## {result.business_name}")
        lines.append(f"- Category: {result.category}")
        lines.append(f"- Website: {result.website}")
        lines.append(f"- City: {result.city}")
        lines.append(f"- Status: {result.status}")
        lines.append(f"- Score: {result.score}")
        lines.append(f"- Opportunity: {result.opportunity_summary}")
        lines.append(f"- Schema types: {', '.join(result.schema_types_found) or 'None'}")
        lines.append(f"- Missing: {', '.join(result.missing_fields) or 'None'}")
        lines.append(f"- Recommended type: {result.recommended_type or 'None'}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
