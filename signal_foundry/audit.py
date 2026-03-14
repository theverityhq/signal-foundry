from __future__ import annotations

import csv
import html
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
HIGH_VALUE_CATEGORIES = {"restaurant", "dentist", "autorepair", "optometrist"}
MEDIUM_VALUE_CATEGORIES = {"barbershop", "landscaper"}


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
                    phone=business.phone,
                    address=business.address,
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
                    phone=business.phone,
                    address=business.address,
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
                    phone=business.phone,
                    address=business.address,
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
    write_html_report(results, output_dir / "audit-report.html")


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
                    "phone": result.phone,
                    "address": result.address,
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
        lines.append(f"- Phone: {result.phone or 'None'}")
        lines.append(f"- Address: {result.address or 'None'}")
        lines.append(f"- City: {result.city}")
        lines.append(f"- Status: {result.status}")
        lines.append(f"- Score: {result.score}")
        lines.append(f"- Opportunity: {result.opportunity_summary}")
        lines.append(f"- Schema types: {', '.join(result.schema_types_found) or 'None'}")
        lines.append(f"- Missing: {', '.join(result.missing_fields) or 'None'}")
        lines.append(f"- Recommended type: {result.recommended_type or 'None'}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html_report(results: list[AuditResult], path: Path) -> None:
    total = len(results)
    reachable = sum(1 for result in results if result.status == "ok")
    unreachable = sum(1 for result in results if result.status == "unreachable")
    average_score = round(sum(result.score for result in results) / total) if total else 0
    ranked_results = sorted(results, key=_lead_sort_key, reverse=True)
    tier_one = sum(1 for result in ranked_results if _lead_priority_tier(result) == "Tier 1")
    cards = "\n".join(_render_result_card(result) for result in ranked_results)
    spotlight = "\n".join(_render_spotlight_item(result) for result in ranked_results[:5])
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
    .status-ok {{ background: var(--accent-soft); color: var(--accent); }}
    .status-unreachable,
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
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <p class="eyebrow">Signal Foundry Prospect View</p>
      <h1>14589 Lead List</h1>
      <p class="subtitle">A clean local view of the current prospect set, including contact details, city/category filters, site status, score, and the schema recommendation generated for each business.</p>
      <div class="stats">
        <div class="stat"><span class="stat-label">Businesses</span><span class="stat-value">{total}</span></div>
        <div class="stat"><span class="stat-label">Reachable</span><span class="stat-value">{reachable}</span></div>
        <div class="stat"><span class="stat-label">Unreachable</span><span class="stat-value">{unreachable}</span></div>
        <div class="stat"><span class="stat-label">Avg Score</span><span class="stat-value">{average_score}</span></div>
        <div class="stat"><span class="stat-label">Tier 1 Leads</span><span class="stat-value">{tier_one}</span></div>
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
        <label for="status-filter">Status</label>
        <select id="status-filter">
          <option value="">All statuses</option>
          <option value="ok">Reachable</option>
          <option value="unreachable">Unreachable</option>
          <option value="no_website">No website</option>
        </select>
      </div>
      <div class="control">
        <label for="priority-filter">Priority</label>
        <select id="priority-filter">
          <option value="">All tiers</option>
          <option value="Tier 1">Tier 1</option>
          <option value="Tier 2">Tier 2</option>
          <option value="Tier 3">Tier 3</option>
        </select>
      </div>
    </section>

    <section class="spotlight">
      <p class="spotlight-title">Best Targets First</p>
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


def _render_result_card(result: AuditResult) -> str:
    priority_tier = _lead_priority_tier(result)
    lead_fit = _lead_fit_score(result)
    lead_tags = _lead_tags(result)
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
            " ".join(lead_tags),
            priority_tier,
        ]
        if part
    )
    schema_pills = _render_pills(result.schema_types_found, fallback="No schema found")
    missing_pills = _render_pills(result.missing_fields, fallback="No missing fields recorded", muted=True)
    pages_pills = _render_pills(result.pages_scanned, fallback="No pages scanned", muted=True)
    notes_pills = _render_pills(result.notes, fallback="No notes", muted=True)
    tag_pills = _render_pills(lead_tags, fallback="No lead tags")
    recommended_jsonld = html.escape(result.recommended_jsonld or "No JSON-LD recommendation was generated.")
    website = (
        f'<a href="{html.escape(result.website)}" target="_blank" rel="noreferrer">{html.escape(result.website)}</a>'
        if result.website
        else "None"
    )
    phone = html.escape(result.phone or "None")
    address = html.escape(result.address or "None")
    city = html.escape(result.city or "Unknown")
    status_label = result.status.replace("_", " ")

    return f"""<article class="card" data-city="{html.escape(result.city)}" data-category="{html.escape(result.category)}" data-status="{html.escape(result.status)}" data-priority="{html.escape(priority_tier)}" data-search="{html.escape(search_text)}">
  <div class="card-top">
    <div>
      <h2>{html.escape(result.business_name)}</h2>
      <div class="meta">
        <span class="chip">{html.escape(result.category)}</span>
        <span class="chip">{city}</span>
        <span class="chip status-{html.escape(result.status)}">{html.escape(status_label)}</span>
        <span class="chip">{html.escape(priority_tier)}</span>
      </div>
    </div>
    <div class="score">
      <span class="score-value">{lead_fit}</span>
      <span class="score-label">Lead Fit</span>
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
      <span class="detail-label">Audit Score</span>
      <div class="detail-value">{result.score}</div>
    </div>
    <div class="detail">
      <span class="detail-label">Recommended Type</span>
      <div class="detail-value">{html.escape(result.recommended_type or 'None')}</div>
    </div>
  </div>
  <div class="summary">{html.escape(result.opportunity_summary or 'No summary available.')}</div>
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
    <span class="detail-label">Notes</span>
    <div class="list-block">{notes_pills}</div>
  </div>
  <details>
    <summary>Recommended JSON-LD</summary>
    <pre>{recommended_jsonld}</pre>
  </details>
</article>"""


def _render_pills(values: list[str], *, fallback: str, muted: bool = False) -> str:
    if not values:
        class_name = "pill pill-muted" if muted else "pill"
        return f'<span class="{class_name}">{html.escape(fallback)}</span>'
    class_name = "pill pill-muted" if muted else "pill"
    return "".join(f'<span class="{class_name}">{html.escape(value)}</span>' for value in values)


def _render_spotlight_item(result: AuditResult) -> str:
    tier = _lead_priority_tier(result)
    fit = _lead_fit_score(result)
    reason = ", ".join(_lead_tags(result)[:2]) or "Prospect fit"
    return (
        f'<div class="spotlight-item"><strong>{html.escape(result.business_name)}</strong>'
        f'<span>{html.escape(result.category)} in {html.escape(result.city or "Unknown")}</span>'
        f'<span>{html.escape(tier)} | Lead fit {fit}</span>'
        f'<span>{html.escape(reason)}</span></div>'
    )


def _lead_sort_key(result: AuditResult) -> tuple[int, int, str]:
    return (_lead_fit_score(result), result.score, result.business_name.lower())


def _lead_priority_tier(result: AuditResult) -> str:
    score = _lead_fit_score(result)
    if score >= 75:
        return "Tier 1"
    if score >= 55:
        return "Tier 2"
    return "Tier 3"


def _lead_fit_score(result: AuditResult) -> int:
    category = result.category.lower().replace(" ", "")
    score = 0

    if result.website:
        score += 20
    if result.phone:
        score += 20
    if result.address:
        score += 15
    if result.city:
        score += 5

    if category in HIGH_VALUE_CATEGORIES:
        score += 20
    elif category in MEDIUM_VALUE_CATEGORIES:
        score += 14
    else:
        score += 10

    if result.status == "ok":
        score += min(20, max(0, 70 - result.score) // 2)
    elif result.status == "unreachable":
        score += 10
    elif result.status == "no_website":
        score -= 10

    if "structured_data" in result.missing_fields or "local_business_schema" in result.missing_fields:
        score += 10
    elif result.status == "unreachable":
        score += 8

    return max(0, min(100, score))


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

    if result.status == "ok":
        if result.score < 50:
            tags.append("Strong fix opportunity")
        else:
            tags.append("Reachable now")
    elif result.status == "unreachable":
        tags.append("Needs live crawl")
    elif result.status == "no_website":
        tags.append("No website")

    if "structured_data" in result.missing_fields or result.status == "unreachable":
        tags.append("Schema audit candidate")

    if not result.phone and not result.address:
        tags.append("Needs contact enrichment")

    return tags[:5]
