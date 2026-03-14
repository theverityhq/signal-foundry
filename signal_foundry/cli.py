from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .audit import audit_businesses, load_businesses, write_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="signal-foundry",
        description="Audit local business sites for schema and AI/search visibility readiness.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to the prospects CSV file.",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory for CSV, JSON, and Markdown report files.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "12")),
        help="HTTP timeout per request.",
    )
    parser.add_argument(
        "--max-pages-per-site",
        type=int,
        default=int(os.getenv("MAX_PAGES_PER_SITE", "6")),
        help="Maximum candidate pages to scan per site.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.getenv("USER_AGENT", "SignalFoundryBot/0.1 (+local audit)"),
        help="User-Agent header for requests.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    businesses = load_businesses(Path(args.input))
    results = audit_businesses(
        businesses,
        user_agent=args.user_agent,
        timeout_seconds=args.timeout_seconds,
        max_pages_per_site=args.max_pages_per_site,
    )
    write_reports(results, Path(args.output_dir))
    logging.info("Wrote %s audit result(s) to %s", len(results), args.output_dir)


if __name__ == "__main__":
    main()
