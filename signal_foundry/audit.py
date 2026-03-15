from __future__ import annotations

import csv
import html
import json
import logging
import re
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
HIGH_VALUE_CATEGORIES = {"restaurant", "dentist", "autorepair", "optometrist"}
MEDIUM_VALUE_CATEGORIES = {"barbershop", "landscaper"}
MAX_FETCH_FAILURES = 4


def load_businesses(input_csv: Path) -> list[Business]:
    with input_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        businesses = []
        for row in reader:
            website = (row.get("website") or "").strip()
            businesses.append(
                Business(
                    name=(row.get("name") or row.get("business_name") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    website=normalize_url(website),
                    phone=(row.get("phone") or "").strip(),
                    address=(row.get("address") or "").strip(),
                    city=(row.get("city") or "").strip(),
                )
            )
    return businesses


def load_audit_results(input_csv: Path) -> list[AuditResult]:
    with input_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        results: list[AuditResult] = []
        for row in reader:
            results.append(
                AuditResult(
                    business_name=(row.get("business_name") or row.get("name") or "").strip(),
                    category=(row.get("category") or "").strip(),
                    website=(row.get("website") or "").strip(),
                    phone=(row.get("phone") or "").strip(),
                    address=(row.get("address") or "").strip(),
                    city=(row.get("city") or "").strip(),
                    prospect_fit=(row.get("prospect_fit") or "").strip() or "Needs Review",
                    prospect_fit_score=_parse_int(row.get("prospect_fit_score"), default=0),
                    status=(row.get("status") or "").strip() or "needs_manual_review",
                    score=_parse_int(row.get("score"), default=0),
                    schema_types_found=_split_csvish(row.get("schema_types_found")),
                    missing_fields=_split_csvish(row.get("missing_fields")),
                    opportunity_summary=(row.get("opportunity_summary") or "").strip(),
                    recommended_type=(row.get("recommended_type") or "").strip(),
                    recommended_jsonld=row.get("recommended_jsonld") or "",
                    current_jsonld=row.get("current_jsonld") or "",
                    schema_gap_summary=(row.get("schema_gap_summary") or "").strip(),
                    recommendation_reasons=_split_piped(row.get("recommendation_reasons")),
                    pages_scanned=_split_csvish(row.get("pages_scanned")),
                    fetch_failures=_split_piped(row.get("fetch_failures")),
                    artifact_paths=_split_piped(row.get("artifact_paths")),
                    notes=_split_piped(row.get("notes")),
                )
            )
    return results


def _parse_int(value: str | None, *, default: int = 0) -> int:
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _split_csvish(value: str | None) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _split_piped(value: str | None) -> list[str]:
    raw = (value or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split("|") if item.strip()]


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


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "business"


def score_prospect_fit(business: Business) -> int:
    category = business.category.lower().replace(" ", "")
    score = 0

    if business.website:
        score += 28
    if business.phone:
        score += 18
    if business.address:
        score += 14
    if business.city:
        score += 5

    if category in HIGH_VALUE_CATEGORIES:
        score += 25
    elif category in MEDIUM_VALUE_CATEGORIES:
        score += 18
    else:
        score += 10

    return max(0, min(100, score))


def classify_prospect_fit(score: int) -> str:
    if score >= 70:
        return "Good Fit"
    if score >= 50:
        return "Needs Review"
    return "Low Fit"


def summarize_request_error(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    request = getattr(exc, "request", None)
    url = getattr(request, "url", "")
    prefix = f"{url}: " if url else ""

    if isinstance(exc, requests.HTTPError) and response is not None:
        return f"{prefix}HTTP {response.status_code}"
    if isinstance(exc, requests.Timeout):
        return f"{prefix}timeout"
    if isinstance(exc, requests.ConnectionError):
        message = str(exc)
        if "Name or service not known" in message or "nodename nor servname" in message or "resolve host" in message:
            return f"{prefix}DNS lookup failed"
        if "SSLError" in message:
            return f"{prefix}SSL handshake failed"
        return f"{prefix}connection failed"
    return f"{prefix}{str(exc) or exc.__class__.__name__}"


def extract_jsonld_blocks(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[Any] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text(strip=False)
        if not raw or not raw.strip():
            continue
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return blocks


def extract_jsonld_types(html: str) -> set[str]:
    found: set[str] = set()
    for payload in extract_jsonld_blocks(html):
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


def build_schema_gap_summary(
    *,
    status: str,
    schema_types: set[str],
    missing_fields: list[str],
    fetch_failures: list[str],
) -> str:
    if status != "verified":
        if fetch_failures:
            return "Live audit was not verified. Review the fetch failures before making claims about current schema."
        return "Live audit was not verified yet."

    if not schema_types:
        return "No JSON-LD schema was detected on the scanned pages."

    if not missing_fields:
        return "Schema was detected and no major gaps were flagged by the current heuristic audit."

    return "Schema is present, but the current implementation appears incomplete for local search and AI readability."


def build_recommendation_reasons(result: AuditResult) -> list[str]:
    reasons: list[str] = []

    if result.status != "verified":
        reasons.append("Run a verified live audit before presenting current-schema claims.")
        reasons.append("Use the proposed schema as a draft implementation plan, not as proof of an existing gap.")
        return reasons

    if "structured_data" in result.missing_fields:
        reasons.append("No JSON-LD was detected, so search engines and AI tools get less explicit business context.")
    if "local_business_schema" in result.missing_fields:
        reasons.append(f"A typed {result.recommended_type or 'LocalBusiness'} schema would better define the business for Google and AI systems.")
    if "hours" in result.missing_fields:
        reasons.append("Opening-hours signals appear weak or missing, which can reduce trust and machine readability.")
    if "phone" in result.missing_fields:
        reasons.append("Telephone markup appears weak or missing, which hurts contact clarity in structured search results.")
    if "address" in result.missing_fields:
        reasons.append("Address signals appear weak or missing, which matters for local entity understanding.")
    if "menu_schema" in result.missing_fields:
        reasons.append("Menu-like content was found without clear menu schema, so rich food-service context may be lost.")
    if "service_schema" in result.missing_fields:
        reasons.append("Service-oriented content was found without service schema, which makes service understanding less explicit.")
    if "product_schema" in result.missing_fields:
        reasons.append("Product-like content was found without product schema, limiting machine-readable offer detail.")

    if not reasons:
        reasons.append("The proposed schema mainly standardizes and strengthens what is already present.")

    return reasons


def write_audit_artifacts(
    result: AuditResult,
    *,
    artifact_dir: Path | None,
    fetched_pages: list[tuple[str, str]],
    jsonld_blocks: list[Any],
) -> list[str]:
    if artifact_dir is None:
        return []

    business_dir = artifact_dir / slugify(result.business_name)
    business_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths: list[str] = []

    if jsonld_blocks:
        current_schema_path = business_dir / "current-schema.json"
        current_schema_path.write_text(json.dumps(jsonld_blocks, indent=2), encoding="utf-8")
        artifact_paths.append(str(current_schema_path.resolve()))

    proposed_schema_path = business_dir / "proposed-schema.json"
    proposed_schema_path.write_text(result.recommended_jsonld or "", encoding="utf-8")
    artifact_paths.append(str(proposed_schema_path.resolve()))

    for index, (url, page_html) in enumerate(fetched_pages, start=1):
        page_path = business_dir / f"page-{index:02d}.html"
        page_path.write_text(page_html, encoding="utf-8")
        artifact_paths.append(str(page_path.resolve()))

    if fetched_pages:
        manifest_path = business_dir / "pages.json"
        manifest_rows = [
            {"url": page_url, "file": f"page-{page_index:02d}.html"}
            for page_index, (page_url, _page_html) in enumerate(fetched_pages, start=1)
        ]
        manifest_path.write_text(json.dumps(manifest_rows, indent=2), encoding="utf-8")
        artifact_paths.append(str(manifest_path.resolve()))

    return artifact_paths


def audit_businesses(
    businesses: list[Business],
    *,
    user_agent: str,
    timeout_seconds: int,
    max_pages_per_site: int,
    skip_live_audit: bool = False,
    artifact_dir: Path | None = None,
) -> list[AuditResult]:
    session = requests.Session()
    results: list[AuditResult] = []

    for business in businesses:
        prospect_fit_score = score_prospect_fit(business)
        prospect_fit = classify_prospect_fit(prospect_fit_score)

        if not business.website:
            results.append(
                AuditResult(
                    business_name=business.name,
                    category=business.category,
                    website="",
                    phone=business.phone,
                    address=business.address,
                    city=business.city,
                    prospect_fit=prospect_fit,
                    prospect_fit_score=prospect_fit_score,
                    status="no_website",
                    score=0,
                    missing_fields=["website"],
                    opportunity_summary="This business does not have a website on file, so it is not a live schema-audit candidate yet.",
                    schema_gap_summary="No site is available to audit.",
                    recommendation_reasons=["Add or confirm the business website before offering a schema audit."],
                    notes=["Prospect fit was scored from category and contact coverage only."],
                )
            )
            continue

        if skip_live_audit:
            schema_type = recommend_schema_type(business, SiteSignals())
            results.append(
                AuditResult(
                    business_name=business.name,
                    category=business.category,
                    website=business.website,
                    phone=business.phone,
                    address=business.address,
                    city=business.city,
                    prospect_fit=prospect_fit,
                    prospect_fit_score=prospect_fit_score,
                    status="needs_manual_review",
                    score=0,
                    opportunity_summary="Prospect was auto-ranked from business fit and contact completeness. Live site audit has not been run yet.",
                    recommended_type=schema_type,
                    recommended_jsonld=recommend_jsonld(business, schema_type),
                    schema_gap_summary="Current schema has not been verified because prospect-only mode skipped the live audit.",
                    recommendation_reasons=build_recommendation_reasons(
                        AuditResult(
                            business_name=business.name,
                            category=business.category,
                            website=business.website,
                            phone=business.phone,
                            address=business.address,
                            city=business.city,
                            prospect_fit=prospect_fit,
                            prospect_fit_score=prospect_fit_score,
                            status="needs_manual_review",
                            score=0,
                            recommended_type=schema_type,
                            recommended_jsonld=recommend_jsonld(business, schema_type),
                        )
                    ),
                    notes=["Prospect-only mode skipped website fetching.", "Run a live audit on shortlisted leads before making schema claims."],
                )
            )
            continue

        schema_types: set[str] = set()
        all_jsonld_blocks: list[Any] = []
        signals = SiteSignals()
        scanned_urls: list[str] = []
        fetched_pages: list[tuple[str, str]] = []
        notes: list[str] = []
        fetch_failures: list[str] = []

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
                failure = summarize_request_error(exc)
                if failure not in fetch_failures and len(fetch_failures) < MAX_FETCH_FAILURES:
                    fetch_failures.append(failure)
                continue

            scanned_urls.append(url)
            fetched_pages.append((url, html))
            page_blocks = extract_jsonld_blocks(html)
            all_jsonld_blocks.extend(page_blocks)
            for payload in page_blocks:
                _walk_types(payload, schema_types)
            signals = merge_signals(signals, detect_signals(html, business))

        if not scanned_urls:
            schema_type = recommend_schema_type(business, signals)
            results.append(
                AuditResult(
                    business_name=business.name,
                    category=business.category,
                    website=business.website,
                    phone=business.phone,
                    address=business.address,
                    city=business.city,
                    prospect_fit=prospect_fit,
                    prospect_fit_score=prospect_fit_score,
                    status="needs_manual_review",
                    score=0,
                    opportunity_summary="Prospect fit looks promising, but the live audit could not be completed in this run.",
                    recommended_type=schema_type,
                    recommended_jsonld=recommend_jsonld(business, schema_type),
                    fetch_failures=fetch_failures,
                    schema_gap_summary=build_schema_gap_summary(
                        status="needs_manual_review",
                        schema_types=schema_types,
                        missing_fields=[],
                        fetch_failures=fetch_failures,
                    ),
                    recommendation_reasons=build_recommendation_reasons(
                        AuditResult(
                            business_name=business.name,
                            category=business.category,
                            website=business.website,
                            phone=business.phone,
                            address=business.address,
                            city=business.city,
                            prospect_fit=prospect_fit,
                            prospect_fit_score=prospect_fit_score,
                            status="needs_manual_review",
                            score=0,
                            recommended_type=schema_type,
                            recommended_jsonld=recommend_jsonld(business, schema_type),
                            fetch_failures=fetch_failures,
                        )
                    ),
                    notes=["No candidate pages returned a successful response.", "Use a browser-assisted or unrestricted live audit before making schema claims."],
                )
            )
            continue

        fetch_failures = []
        score, missing_fields, summary = score_business(schema_types, signals)
        schema_type = recommend_schema_type(business, signals)
        notes.append(f"Scanned {len(scanned_urls)} page(s).")
        if not schema_types:
            notes.append("No JSON-LD detected on scanned pages.")

        result = AuditResult(
            business_name=business.name,
            category=business.category,
            website=business.website,
            phone=business.phone,
            address=business.address,
            city=business.city,
            prospect_fit=prospect_fit,
            prospect_fit_score=prospect_fit_score,
            status="verified",
            score=score,
            schema_types_found=sorted(schema_types),
            missing_fields=missing_fields,
            opportunity_summary=summary,
            recommended_type=schema_type,
            recommended_jsonld=recommend_jsonld(business, schema_type),
            current_jsonld=json.dumps(all_jsonld_blocks, indent=2) if all_jsonld_blocks else "",
            schema_gap_summary=build_schema_gap_summary(
                status="verified",
                schema_types=schema_types,
                missing_fields=missing_fields,
                fetch_failures=fetch_failures,
            ),
            pages_scanned=scanned_urls,
            fetch_failures=fetch_failures,
            notes=notes,
        )
        result.recommendation_reasons = build_recommendation_reasons(result)
        result.artifact_paths = write_audit_artifacts(
            result,
            artifact_dir=artifact_dir,
            fetched_pages=fetched_pages,
            jsonld_blocks=all_jsonld_blocks,
        )
        results.append(result)

    return results


def rank_results(results: list[AuditResult], limit: int | None = None) -> list[AuditResult]:
    ranked = sorted(results, key=_lead_sort_key, reverse=True)
    if limit is None or limit <= 0:
        return ranked
    return ranked[:limit]


def write_reports(results: list[AuditResult], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json_report(results, output_dir / "audit-report.json")
    write_csv_report(results, output_dir / "audit-report.csv")
    write_markdown_report(results, output_dir / "audit-report.md")
    write_html_report(results, output_dir / "audit-report.html")
    write_outreach_exports(results, output_dir)


def write_json_report(results: list[AuditResult], path: Path) -> None:
    rows = [asdict(result) for result in results]
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def write_csv_report(results: list[AuditResult], path: Path) -> None:
    fieldnames = [
        "business_name",
        "category",
        "website",
        "phone",
        "address",
        "city",
        "prospect_fit",
        "prospect_fit_score",
        "status",
        "score",
        "schema_types_found",
        "missing_fields",
        "opportunity_summary",
        "schema_gap_summary",
        "recommendation_reasons",
        "recommended_type",
        "current_jsonld",
        "recommended_jsonld",
        "pages_scanned",
        "fetch_failures",
        "artifact_paths",
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
                    "phone": result.phone,
                    "address": result.address,
                    "city": result.city,
                    "prospect_fit": result.prospect_fit,
                    "prospect_fit_score": result.prospect_fit_score,
                    "status": result.status,
                    "score": result.score,
                    "schema_types_found": ", ".join(result.schema_types_found),
                    "missing_fields": ", ".join(result.missing_fields),
                    "opportunity_summary": result.opportunity_summary,
                    "schema_gap_summary": result.schema_gap_summary,
                    "recommendation_reasons": " | ".join(result.recommendation_reasons),
                    "recommended_type": result.recommended_type,
                    "current_jsonld": result.current_jsonld,
                    "recommended_jsonld": result.recommended_jsonld,
                    "pages_scanned": ", ".join(result.pages_scanned),
                    "fetch_failures": " | ".join(result.fetch_failures),
                    "artifact_paths": " | ".join(result.artifact_paths),
                    "notes": " | ".join(result.notes),
                }
            )


def write_markdown_report(results: list[AuditResult], path: Path) -> None:
    lines = ["# Signal Foundry Audit Report", ""]
    for result in results:
        lines.append(f"## {result.business_name}")
        lines.append(f"- Category: {result.category}")
        lines.append(f"- Website: {result.website}")
        lines.append(f"- Phone: {result.phone or 'None'}")
        lines.append(f"- Address: {result.address or 'None'}")
        lines.append(f"- City: {result.city}")
        lines.append(f"- Prospect fit: {result.prospect_fit} ({result.prospect_fit_score})")
        lines.append(f"- Live audit: {_audit_status_label(result.status)}")
        lines.append(f"- Audit score: {result.score if result.status == 'verified' else 'Not verified'}")
        lines.append(f"- Opportunity: {result.opportunity_summary}")
        lines.append(f"- Schema gap summary: {result.schema_gap_summary or 'None'}")
        lines.append(
            f"- Schema types: {', '.join(result.schema_types_found) or ('Not verified yet' if result.status != 'verified' else 'None')}"
        )
        lines.append(
            f"- Missing: {', '.join(result.missing_fields) or ('Not verified yet' if result.status != 'verified' else 'None')}"
        )
        lines.append(f"- Recommendation reasons: {' | '.join(result.recommendation_reasons) or 'None'}")
        lines.append(f"- Recommended type: {result.recommended_type or 'None'}")
        lines.append(f"- Fetch failures: {' | '.join(result.fetch_failures) or 'None'}")
        lines.append(f"- Artifacts: {' | '.join(result.artifact_paths) or 'None'}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_outreach_exports(results: list[AuditResult], output_dir: Path, limit: int = 5) -> None:
    candidates = select_outreach_candidates(results, limit=limit)
    if not candidates:
        return
    write_outreach_csv(candidates, output_dir / "top-5-outreach.csv")
    write_outreach_markdown(candidates, output_dir / "top-5-outreach.md")


def select_outreach_candidates(results: list[AuditResult], *, limit: int = 5) -> list[AuditResult]:
    verified = [result for result in results if result.status == "verified"]
    pool = verified if verified else [result for result in results if result.status != "no_website"]
    ranked = sorted(pool, key=_outreach_sort_key, reverse=True)
    return ranked[:limit]


def write_outreach_csv(results: list[AuditResult], path: Path) -> None:
    fieldnames = [
        "rank",
        "business_name",
        "category",
        "city",
        "website",
        "phone",
        "status",
        "audit_score",
        "outreach_score",
        "outreach_tier",
        "primary_gap",
        "recommended_type",
        "outreach_angle",
        "recommendation_reasons",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, result in enumerate(results, start=1):
            writer.writerow(
                {
                    "rank": index,
                    "business_name": result.business_name,
                    "category": result.category,
                    "city": result.city,
                    "website": result.website,
                    "phone": result.phone,
                    "status": _audit_status_label(result.status),
                    "audit_score": result.score if result.status == "verified" else "",
                    "outreach_score": _outreach_score(result),
                    "outreach_tier": _outreach_tier(result),
                    "primary_gap": _primary_gap(result),
                    "recommended_type": result.recommended_type,
                    "outreach_angle": _outreach_angle(result),
                    "recommendation_reasons": " | ".join(result.recommendation_reasons),
                }
            )


def write_outreach_markdown(results: list[AuditResult], path: Path) -> None:
    lines = ["# Top Outreach Targets", ""]
    for index, result in enumerate(results, start=1):
        lines.append(f"## {index}. {result.business_name}")
        lines.append(f"- Category: {result.category}")
        lines.append(f"- City: {result.city}")
        lines.append(f"- Website: {result.website}")
        lines.append(f"- Phone: {result.phone or 'None'}")
        lines.append(f"- Live audit: {_audit_status_label(result.status)}")
        lines.append(f"- Audit score: {result.score if result.status == 'verified' else 'Not verified'}")
        lines.append(f"- Outreach score: {_outreach_score(result)}")
        lines.append(f"- Outreach tier: {_outreach_tier(result)}")
        lines.append(f"- Primary gap: {_primary_gap(result)}")
        lines.append(f"- Outreach angle: {_outreach_angle(result)}")
        lines.append(f"- Recommended type: {result.recommended_type or 'None'}")
        lines.append(f"- Recommendation reasons: {' | '.join(result.recommendation_reasons) or 'None'}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html_report(results: list[AuditResult], path: Path) -> None:
    total = len(results)
    verified = sum(1 for result in results if result.status == "verified")
    manual_review = sum(1 for result in results if result.status == "needs_manual_review")
    average_score = (
        round(sum(result.score for result in results if result.status == "verified") / verified)
        if verified
        else 0
    )
    ranked_results = sorted(results, key=_lead_sort_key, reverse=True)
    tier_one = sum(1 for result in ranked_results if result.prospect_fit == "Good Fit")
    outreach_candidates = select_outreach_candidates(results, limit=5)
    outreach_lookup = {result.business_name: index for index, result in enumerate(outreach_candidates, start=1)}
    cards = "\n".join(_render_result_card(result, outreach_rank=outreach_lookup.get(result.business_name)) for result in ranked_results)
    spotlight = "\n".join(_render_spotlight_item(result, outreach_rank=index) for index, result in enumerate(outreach_candidates, start=1))
    html_document = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Signal Foundry Audit Report</title>
  <style>
    :root {{
      --bg: #f5efe4;
      --panel: rgba(255, 251, 245, 0.86);
      --panel-strong: #fffdf8;
      --ink: #1d2733;
      --muted: #5e6b78;
      --line: rgba(29, 39, 51, 0.12);
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --warn: #b45309;
      --warn-soft: rgba(180, 83, 9, 0.12);
      --bad: #b91c1c;
      --bad-soft: rgba(185, 28, 28, 0.12);
      --shadow: 0 22px 50px rgba(61, 39, 14, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(191, 219, 254, 0.45), transparent 32%),
        radial-gradient(circle at top right, rgba(251, 191, 36, 0.26), transparent 28%),
        linear-gradient(180deg, #fbf7ef 0%, var(--bg) 100%);
      font-family: Georgia, "Avenir Next", serif;
    }}
    a {{
      color: inherit;
    }}
    .shell {{
      width: min(1200px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }}
    .hero {{
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 28px;
      background: linear-gradient(140deg, rgba(255,255,255,0.9), rgba(247, 240, 228, 0.88));
      box-shadow: var(--shadow);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      font: 600 12px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      color: var(--muted);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2.4rem, 5vw, 4.5rem);
      line-height: 0.94;
    }}
    .subtitle {{
      width: min(760px, 100%);
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 1.02rem;
      line-height: 1.6;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
      margin-top: 22px;
    }}
    .stat {{
      padding: 16px 18px;
      border-radius: 20px;
      background: var(--panel);
      border: 1px solid var(--line);
      backdrop-filter: blur(8px);
    }}
    .stat-label {{
      display: block;
      margin-bottom: 6px;
      font: 600 12px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .stat-value {{
      font-size: 2rem;
      line-height: 1;
    }}
    .toolbar {{
      display: grid;
      grid-template-columns: 1.7fr repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin: 22px 0 18px;
    }}
    .control {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .control label {{
      font: 600 12px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .control input,
    .control select {{
      width: 100%;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.82);
      color: var(--ink);
      font: 500 15px/1.3 "Avenir Next", "Trebuchet MS", sans-serif;
    }}
    .results-meta {{
      margin: 10px 0 18px;
      color: var(--muted);
      font: 600 13px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .spotlight {{
      margin: 18px 0 16px;
      padding: 18px;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: rgba(255, 253, 248, 0.88);
      box-shadow: 0 12px 28px rgba(61, 39, 14, 0.06);
    }}
    .spotlight-title {{
      margin: 0 0 12px;
      color: var(--muted);
      font: 600 12px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .spotlight-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
    }}
    .spotlight-item {{
      padding: 14px;
      border-radius: 18px;
      background: linear-gradient(180deg, #fff9ef, #f8eedf);
      border: 1px solid rgba(29, 39, 51, 0.08);
    }}
    .spotlight-item strong {{
      display: block;
      margin-bottom: 6px;
      font-size: 1rem;
      line-height: 1.2;
    }}
    .spotlight-item span {{
      display: block;
      color: var(--muted);
      font: 500 13px/1.45 "Avenir Next", "Trebuchet MS", sans-serif;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
      gap: 16px;
    }}
    .card {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      padding: 18px;
      border-radius: 24px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      box-shadow: 0 10px 26px rgba(61, 39, 14, 0.08);
    }}
    .card-top {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .card h2 {{
      margin: 0;
      font-size: 1.35rem;
      line-height: 1.05;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: #f1ebe1;
      color: var(--muted);
      font: 600 12px/1 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .status-verified {{ background: var(--accent-soft); color: var(--accent); }}
    .status-needs_manual_review {{ background: var(--warn-soft); color: var(--warn); }}
    .status-no_website {{ background: var(--bad-soft); color: var(--bad); }}
    .score {{
      min-width: 70px;
      padding: 12px 10px;
      border-radius: 18px;
      text-align: center;
      background: linear-gradient(180deg, #fff8eb, #f4ead7);
      border: 1px solid var(--line);
    }}
    .score-value {{
      display: block;
      font-size: 1.6rem;
      line-height: 1;
    }}
    .score-label {{
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font: 600 11px/1.2 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .details {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .detail {{
      padding: 12px 14px;
      border-radius: 16px;
      background: #faf6ee;
      border: 1px solid rgba(29, 39, 51, 0.08);
    }}
    .detail-label {{
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font: 600 11px/1.3 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .detail-value {{
      font-size: 0.97rem;
      line-height: 1.5;
      word-break: break-word;
    }}
    .list-block {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      background: #eef6f5;
      color: #165e57;
      font: 600 12px/1.1 "Avenir Next", "Trebuchet MS", sans-serif;
    }}
    .pill-muted {{
      background: #f4ede2;
      color: #75634d;
    }}
    .summary {{
      padding: 14px 16px;
      border-radius: 16px;
      background: #f8f2e7;
      border-left: 4px solid #c0843d;
      line-height: 1.6;
    }}
    .comparison-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .code-panel {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: #fffaf1;
      overflow: hidden;
    }}
    .code-panel-header {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font: 600 11px/1.3 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.1em;
      text-transform: uppercase;
    }}
    .code-panel pre {{
      margin: 0;
      padding: 14px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font: 500 12px/1.6 "SFMono-Regular", Consolas, monospace;
      color: #20303a;
    }}
    .artifact-links {{
      display: grid;
      gap: 8px;
    }}
    .artifact-links a {{
      display: block;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(29, 39, 51, 0.08);
      background: #faf6ee;
      text-decoration: none;
      font: 500 14px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
    }}
    details {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fffaf1;
      overflow: hidden;
    }}
    summary {{
      cursor: pointer;
      list-style: none;
      padding: 12px 14px;
      font: 600 13px/1.4 "Avenir Next", "Trebuchet MS", sans-serif;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    details pre {{
      margin: 0;
      padding: 0 14px 14px;
      overflow-x: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font: 500 12px/1.6 "SFMono-Regular", Consolas, monospace;
      color: #20303a;
    }}
    .empty {{
      display: none;
      margin-top: 18px;
      padding: 20px;
      border-radius: 20px;
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
      background: rgba(255,255,255,0.5);
    }}
    @media (max-width: 900px) {{
      .toolbar {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 640px) {{
      .shell {{
        width: min(100vw - 20px, 100%);
        padding-top: 18px;
      }}
      .hero {{
        padding: 22px 18px;
        border-radius: 22px;
      }}
      .details {{
        grid-template-columns: 1fr;
      }}
      .comparison-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <p class="eyebrow">Signal Foundry Prospect View</p>
      <h1>14589 Lead List</h1>
      <p class="subtitle">A clean local view of the current prospect set, separating prospect fit from live audit verification so failed crawls do not get mistaken for weak leads.</p>
      <div class="stats">
        <div class="stat"><span class="stat-label">Businesses</span><span class="stat-value">{total}</span></div>
        <div class="stat"><span class="stat-label">Audit Verified</span><span class="stat-value">{verified}</span></div>
        <div class="stat"><span class="stat-label">Manual Review</span><span class="stat-value">{manual_review}</span></div>
        <div class="stat"><span class="stat-label">Avg Verified Audit</span><span class="stat-value">{average_score}</span></div>
        <div class="stat"><span class="stat-label">Good-Fit Leads</span><span class="stat-value">{tier_one}</span></div>
      </div>
    </section>

    <section class="toolbar">
      <div class="control">
        <label for="search">Search</label>
        <input id="search" type="search" placeholder="Business, city, category, phone, address">
      </div>
      <div class="control">
        <label for="city-filter">City</label>
        <select id="city-filter">
          <option value="">All cities</option>
        </select>
      </div>
      <div class="control">
        <label for="category-filter">Category</label>
        <select id="category-filter">
          <option value="">All categories</option>
        </select>
      </div>
      <div class="control">
        <label for="status-filter">Live Audit</label>
        <select id="status-filter">
          <option value="">All audit states</option>
          <option value="verified">Verified</option>
          <option value="needs_manual_review">Needs manual review</option>
          <option value="no_website">No website</option>
        </select>
      </div>
      <div class="control">
        <label for="priority-filter">Prospect Fit</label>
        <select id="priority-filter">
          <option value="">All fit levels</option>
          <option value="Good Fit">Good fit</option>
          <option value="Needs Review">Needs review</option>
          <option value="Low Fit">Low fit</option>
        </select>
      </div>
    </section>

    <section class="spotlight">
      <p class="spotlight-title">Best Outreach Targets</p>
      <div class="spotlight-grid">
        {spotlight}
      </div>
    </section>

    <div id="results-meta" class="results-meta"></div>
    <section id="card-grid" class="grid">
      {cards}
    </section>
    <div id="empty" class="empty">No businesses match the current filters.</div>
  </div>
  <script>
    const cards = [...document.querySelectorAll('.card')];
    const cityFilter = document.getElementById('city-filter');
    const categoryFilter = document.getElementById('category-filter');
    const statusFilter = document.getElementById('status-filter');
    const priorityFilter = document.getElementById('priority-filter');
    const search = document.getElementById('search');
    const resultsMeta = document.getElementById('results-meta');
    const empty = document.getElementById('empty');

    function fillSelect(select, attribute) {{
      const values = [...new Set(cards.map(card => card.dataset[attribute]).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      for (const value of values) {{
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value;
        select.appendChild(option);
      }}
    }}

    function applyFilters() {{
      const city = cityFilter.value.toLowerCase();
      const category = categoryFilter.value.toLowerCase();
      const status = statusFilter.value.toLowerCase();
      const priority = priorityFilter.value.toLowerCase();
      const query = search.value.trim().toLowerCase();
      let visible = 0;

      for (const card of cards) {{
        const matchesCity = !city || card.dataset.city.toLowerCase() === city;
        const matchesCategory = !category || card.dataset.category.toLowerCase() === category;
        const matchesStatus = !status || card.dataset.status.toLowerCase() === status;
        const matchesPriority = !priority || card.dataset.priority.toLowerCase() === priority;
        const haystack = card.dataset.search.toLowerCase();
        const matchesQuery = !query || haystack.includes(query);
        const show = matchesCity && matchesCategory && matchesStatus && matchesPriority && matchesQuery;
        card.style.display = show ? '' : 'none';
        if (show) visible += 1;
      }}

      resultsMeta.textContent = `${{visible}} of {total} businesses shown`;
      empty.style.display = visible ? 'none' : 'block';
    }}

    fillSelect(cityFilter, 'city');
    fillSelect(categoryFilter, 'category');
    for (const element of [cityFilter, categoryFilter, statusFilter, priorityFilter, search]) {{
      element.addEventListener('input', applyFilters);
      element.addEventListener('change', applyFilters);
    }}
    applyFilters();
  </script>
</body>
</html>
"""
    path.write_text(html_document, encoding="utf-8")


def _render_result_card(result: AuditResult, *, outreach_rank: int | None = None) -> str:
    lead_tags = _lead_tags(result)
    audit_status_label = _audit_status_label(result.status)
    outreach_score = _outreach_score(result)
    outreach_tier = _outreach_tier(result)
    outreach_angle = _outreach_angle(result)
    search_text = " ".join(
        part
        for part in [
            result.business_name,
            result.category,
            result.website,
            result.phone,
            result.address,
            result.city,
            result.opportunity_summary,
            " ".join(result.missing_fields),
            " ".join(result.schema_types_found),
            " ".join(result.fetch_failures),
            " ".join(lead_tags),
            result.prospect_fit,
            audit_status_label,
            outreach_tier,
            outreach_angle,
        ]
        if part
    )
    schema_pills = _render_pills(
        result.schema_types_found,
        fallback="Not verified yet" if result.status != "verified" else "No schema found",
    )
    missing_pills = _render_pills(
        result.missing_fields,
        fallback="Not verified yet" if result.status != "verified" else "No missing fields recorded",
        muted=True,
    )
    pages_pills = _render_pills(result.pages_scanned, fallback="No pages scanned", muted=True)
    notes_pills = _render_pills(result.notes, fallback="No notes", muted=True)
    fetch_failure_pills = _render_pills(result.fetch_failures, fallback="None", muted=True)
    tag_pills = _render_pills(lead_tags, fallback="No lead tags")
    reason_pills = _render_pills(result.recommendation_reasons, fallback="No recommendation reasons yet", muted=True)
    artifact_links = _render_artifact_links(result.artifact_paths)
    current_jsonld = html.escape(result.current_jsonld or "Current schema not captured yet.")
    recommended_jsonld = html.escape(result.recommended_jsonld or "No JSON-LD recommendation was generated.")
    website = (
        f'<a href="{html.escape(result.website)}" target="_blank" rel="noreferrer">{html.escape(result.website)}</a>'
        if result.website
        else "None"
    )
    phone = html.escape(result.phone or "None")
    address = html.escape(result.address or "None")
    city = html.escape(result.city or "Unknown")
    audit_score = str(result.score) if result.status == "verified" else "Not verified"

    return f"""<article class="card" data-city="{html.escape(result.city)}" data-category="{html.escape(result.category)}" data-status="{html.escape(result.status)}" data-priority="{html.escape(result.prospect_fit)}" data-search="{html.escape(search_text)}">
  <div class="card-top">
    <div>
      <h2>{html.escape(result.business_name)}</h2>
      <div class="meta">
        <span class="chip">{html.escape(result.category)}</span>
        <span class="chip">{city}</span>
        <span class="chip status-{html.escape(result.status)}">{html.escape(audit_status_label)}</span>
        <span class="chip">{html.escape(result.prospect_fit)}</span>
        <span class="chip">{html.escape(outreach_tier)}</span>
        {f'<span class="chip">Outreach #{outreach_rank}</span>' if outreach_rank else ''}
      </div>
    </div>
    <div class="score">
      <span class="score-value">{outreach_score}</span>
      <span class="score-label">Outreach</span>
    </div>
  </div>
  <div class="details">
    <div class="detail">
      <span class="detail-label">Website</span>
      <div class="detail-value">{website}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Phone</span>
      <div class="detail-value">{phone}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Address</span>
      <div class="detail-value">{address}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Live Audit</span>
      <div class="detail-value">{html.escape(audit_status_label)}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Audit Score</span>
      <div class="detail-value">{audit_score}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Prospect Fit</span>
      <div class="detail-value">{result.prospect_fit_score}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Recommended Type</span>
      <div class="detail-value">{html.escape(result.recommended_type or 'None')}</div>
    </div>
  </div>
  <div class="summary">{html.escape(result.opportunity_summary or 'No summary available.')}</div>
  <div class="detail">
    <span class="detail-label">Outreach Angle</span>
    <div class="detail-value">{html.escape(outreach_angle)}</div>
  </div>
  <div class="detail">
    <span class="detail-label">Schema Gap Summary</span>
    <div class="detail-value">{html.escape(result.schema_gap_summary or 'None')}</div>
  </div>
  <div>
    <span class="detail-label">Lead Tags</span>
    <div class="list-block">{tag_pills}</div>
  </div>
  <div>
    <span class="detail-label">Schema Types</span>
    <div class="list-block">{schema_pills}</div>
  </div>
  <div>
    <span class="detail-label">Missing Signals</span>
    <div class="list-block">{missing_pills}</div>
  </div>
  <div>
    <span class="detail-label">Pages Scanned</span>
    <div class="list-block">{pages_pills}</div>
  </div>
  <div>
    <span class="detail-label">Fetch Failures</span>
    <div class="list-block">{fetch_failure_pills}</div>
  </div>
  <div>
    <span class="detail-label">Notes</span>
    <div class="list-block">{notes_pills}</div>
  </div>
  <div>
    <span class="detail-label">Recommendation Reasons</span>
    <div class="list-block">{reason_pills}</div>
  </div>
  <div class="comparison-grid">
    <section class="code-panel">
      <div class="code-panel-header">Current Schema</div>
      <pre>{current_jsonld}</pre>
    </section>
    <section class="code-panel">
      <div class="code-panel-header">Proposed Schema</div>
      <pre>{recommended_jsonld}</pre>
    </section>
  </div>
  <div>
    <span class="detail-label">Audit Artifacts</span>
    <div class="artifact-links">{artifact_links}</div>
  </div>
</article>"""


def _render_pills(values: list[str], *, fallback: str, muted: bool = False) -> str:
    if not values:
        class_name = "pill pill-muted" if muted else "pill"
        return f'<span class="{class_name}">{html.escape(fallback)}</span>'
    class_name = "pill pill-muted" if muted else "pill"
    return "".join(f'<span class="{class_name}">{html.escape(value)}</span>' for value in values)


def _render_artifact_links(paths: list[str]) -> str:
    if not paths:
        return '<span class="pill pill-muted">No artifacts saved</span>'
    return "".join(
        f'<a href="{html.escape(Path(path).as_uri() if Path(path).is_absolute() else path)}" target="_blank" rel="noreferrer">{html.escape(Path(path).name if Path(path).is_absolute() else path)}</a>'
        for path in paths
    )


def _render_spotlight_item(result: AuditResult, *, outreach_rank: int | None = None) -> str:
    fit = result.prospect_fit_score
    reason = ", ".join(_lead_tags(result)[:2]) or "Prospect fit"
    return (
        f'<div class="spotlight-item"><strong>{html.escape(result.business_name)}</strong>'
        f'<span>{html.escape(result.category)} in {html.escape(result.city or "Unknown")}</span>'
        f'<span>{html.escape(_outreach_tier(result))} | Outreach score {_outreach_score(result)}</span>'
        f'<span>{"Outreach #" + str(outreach_rank) + " | " if outreach_rank else ""}{html.escape(reason)}</span></div>'
    )


def _lead_sort_key(result: AuditResult) -> tuple[int, int, str]:
    verified_bonus = 1 if result.status == "verified" else 0
    return (result.prospect_fit_score, verified_bonus, result.score, result.business_name.lower())


def _outreach_sort_key(result: AuditResult) -> tuple[int, int, int, str]:
    verified_bonus = 1 if result.status == "verified" else 0
    return (_outreach_score(result), verified_bonus, result.prospect_fit_score, result.business_name.lower())


def _outreach_score(result: AuditResult) -> int:
    score = 0
    if result.status == "verified":
        score += max(0, 100 - result.score)
    elif result.status == "needs_manual_review":
        score += 30

    missing = set(result.missing_fields)
    if "structured_data" in missing:
        score += 32
    if "local_business_schema" in missing:
        score += 22
    if "menu_schema" in missing:
        score += 10
    if "service_schema" in missing:
        score += 10
    if "product_schema" in missing:
        score += 8
    if result.prospect_fit == "Good Fit":
        score += 12
    elif result.prospect_fit == "Needs Review":
        score += 6
    return min(200, score)


def _outreach_tier(result: AuditResult) -> str:
    score = _outreach_score(result)
    if score >= 110:
        return "Best First"
    if score >= 70:
        return "Strong Pitch"
    if score >= 35:
        return "Secondary"
    return "Low Priority"


def _primary_gap(result: AuditResult) -> str:
    missing = set(result.missing_fields)
    if "structured_data" in missing:
        return "No JSON-LD detected"
    if "local_business_schema" in missing:
        return "Wrong or missing local business type"
    if "menu_schema" in missing:
        return "Menu/service content lacks schema"
    if "service_schema" in missing:
        return "Service content lacks schema"
    if "product_schema" in missing:
        return "Product content lacks schema"
    if result.status == "needs_manual_review":
        return "Needs live verification"
    return "Smaller cleanup opportunity"


def _outreach_angle(result: AuditResult) -> str:
    primary_gap = _primary_gap(result)
    category = result.category or "business"
    if primary_gap == "No JSON-LD detected":
        return f"This {category.lower()} site appears to have no machine-readable schema, so the pitch is a clean before-and-after visibility fix."
    if primary_gap == "Wrong or missing local business type":
        return f"This {category.lower()} already has some schema, but it is not clearly typed for its local business category."
    if "lacks schema" in primary_gap:
        return f"This {category.lower()} has content that search engines can read visually, but not enough schema to understand it cleanly."
    if primary_gap == "Needs live verification":
        return f"This {category.lower()} looks like a strong prospect, but the pitch should wait until the site is manually verified."
    return f"This {category.lower()} looks mostly solid; the angle is standardization and strengthening rather than a major fix."


def _lead_tags(result: AuditResult) -> list[str]:
    tags: list[str] = []
    category = result.category.lower().replace(" ", "")

    if category in HIGH_VALUE_CATEGORIES:
        tags.append("High-value vertical")
    elif category in MEDIUM_VALUE_CATEGORIES:
        tags.append("Steady local category")

    if result.phone:
        tags.append("Phone listed")
    if result.address:
        tags.append("Address listed")
    if result.website:
        tags.append("Website present")

    if result.status == "verified":
        if result.score < 50:
            tags.append("Verified fix opportunity")
        else:
            tags.append("Audit verified")
    elif result.status == "needs_manual_review":
        tags.append("Needs live audit")
    elif result.status == "no_website":
        tags.append("No website")

    if "structured_data" in result.missing_fields:
        tags.append("Schema audit candidate")
    elif result.status == "needs_manual_review":
        tags.append("Schema check pending")

    if not result.phone and not result.address:
        tags.append("Needs contact enrichment")

    return tags[:5]


def _audit_status_label(status: str) -> str:
    labels = {
        "verified": "Verified",
        "needs_manual_review": "Needs Manual Review",
        "no_website": "No Website",
    }
    return labels.get(status, status.replace("_", " ").title())
