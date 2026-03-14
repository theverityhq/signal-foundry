# Signal Foundry

Signal Foundry is a local-business AI/search visibility audit tool.

The MVP audits a CSV of business websites and reports:

- whether the site is reachable
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

This writes:

- `output/audit-report.csv`
- `output/audit-report.json`
- `output/audit-report.md`
- `output/audit-report.html`

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

## Next steps

1. Add Places/provider abstraction for radius-based discovery.
2. Add robots and crawlability checks.
3. Add microdata and RDFa extraction.
4. Add richer schema templates by business type.
5. Add outreach report generation and CRM export.
