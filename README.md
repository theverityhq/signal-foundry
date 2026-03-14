# Signal Foundry

Signal Foundry is a local-business AI/search visibility audit tool.

The MVP audits a CSV of business websites and reports:

- whether the site is reachable
- whether the live audit was actually verified or still needs manual review
- prospect-fit scoring that stays separate from crawl success
- whether JSON-LD structured data is present
- which schema types were detected
- which high-value signals appear missing
- a rough readiness score
- a recommended schema template to propose in a sales conversation
- a browsable HTML dashboard for prospect review, prioritization, and filtering

## Why this exists

Most local business sites are written for humans only. Search engines and AI systems rely on machine-readable data to understand business identity, services, menu items, hours, and contact details. This repo helps identify weak sites and generate a credible audit report.

## Project layout

```text
signal-foundry/
├── docs/
│   └── product-spec.md
├── data/
│   └── prospects.example.csv
├── output/
├── pyproject.toml
├── .env.example
└── signal_foundry/
    ├── __init__.py
    ├── __main__.py
    ├── audit.py
    ├── cli.py
    └── models.py
```

## Install

```bash
cd /Users/dad/Desktop/Projects/signal-foundry
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

## Run

```bash
python3 -m signal_foundry \
  --input data/prospects.example.csv \
  --output-dir output
```

Bulk prospect triage without crawling every site:

```bash
python3 -m signal_foundry \
  --input data/prospects-14589-initial.csv \
  --output-dir output/14589-batch \
  --prospect-only \
  --top-n 10
```

This writes:

- `output/audit-report.csv`
- `output/audit-report.json`
- `output/audit-report.md`
- `output/audit-report.html`
- optionally `output/top-N/*` when `--top-n` is used

Recommended workflow:

1. Run `--prospect-only --top-n 10` on a large lead list.
2. Review the generated `top-10/audit-report.html`.
3. Run a live audit only on those shortlisted businesses from a network-enabled environment.

## Input format

CSV columns:

- `name`
- `category`
- `website`
- `phone` (optional)
- `address` (optional)
- `city` (optional)

## Current MVP scope

- CSV input, not radius-based discovery yet
- JSON-LD extraction only
- lightweight page discovery: homepage, about, contact, services, menu, products
- heuristic scoring
- CSV, JSON, Markdown, and HTML outputs
- sales-oriented HTML view with lead-fit ranking, tags, and best-target spotlighting
- fetch failure capture so blocked audits show the reason instead of being mistaken for weak leads
- prospect-only batch mode for large lead lists
- top-N shortlist output so manual review is limited to the best candidates

## Next steps

1. Add Places/provider abstraction for radius-based discovery.
2. Add robots and crawlability checks.
3. Add microdata and RDFa extraction.
4. Add richer schema templates by business type.
5. Add outreach report generation and CRM export.
