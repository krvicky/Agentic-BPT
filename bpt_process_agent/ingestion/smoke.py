"""Explicitly opt-in, potentially paid API smoke command; never collected by pytest."""
import argparse
from pathlib import Path
from .cli import main as extract_main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optional real OpenRouter smoke test (may incur charges)")
    parser.add_argument("--allow-paid-api", action="store_true", required=True)
    parser.add_argument("document", type=Path)
    parser.add_argument("--store", default="smoke-evidence-store")
    parser.add_argument("--output", default="smoke-evidence-output")
    args = parser.parse_args(argv)
    return extract_main([str(args.document), "--store", args.store, "--output", args.output, "--max-calls", "8"])

if __name__ == "__main__":
    raise SystemExit(main())
