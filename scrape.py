#!/usr/bin/env python3
"""CLI entry point for the Supreme Court judgments scraper.

Usage:
    uv run python scrape.py --start-date 2024-01-02 --end-date 2024-01-03
    uv run python scrape.py --retry-failures            # retry only prior misses
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from src import config
from src.runner import retry_failures, run_scrape


def _valid_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date {value!r}, expected YYYY-MM-DD")
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=_valid_date,
                        help="Start date (inclusive), YYYY-MM-DD")
    parser.add_argument("--end-date", type=_valid_date,
                        help="End date (inclusive), YYYY-MM-DD")
    parser.add_argument("--day-step", type=int, default=30,
                        help="Days per search chunk (default: 30)")
    parser.add_argument("--output-dir", type=Path, default=config.DEFAULT_OUTPUT_DIR,
                        help="Output directory (default: ./data)")
    parser.add_argument("--delay", type=float, default=config.REQUEST_DELAY,
                        help=f"Base seconds between requests (default: {config.REQUEST_DELAY})")
    parser.add_argument("--retry-failures", action="store_true",
                        help="Retry only records in <output-dir>/failures.json")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.retry_failures:
        stats = retry_failures(output_dir=args.output_dir, delay=args.delay)
    else:
        if not args.start_date or not args.end_date:
            print("--start-date and --end-date are required (or use --retry-failures)",
                  file=sys.stderr)
            return 2
        if args.start_date > args.end_date:
            print("start-date must be <= end-date", file=sys.stderr)
            return 2
        stats = run_scrape(
            start_date=args.start_date,
            end_date=args.end_date,
            day_step=args.day_step,
            output_dir=args.output_dir,
            delay=args.delay,
        )

    print(f"\nSummary: {stats.summary()}")
    if stats.aborted:
        print("Run was aborted early (likely throttled) — see the warning above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
