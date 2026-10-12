"""Standalone ingestion CLI; graph state is never opened or modified."""
import argparse
from pathlib import Path
import sys
from dotenv import load_dotenv
from ..llm_config import LLMConfig
from .llm import OpenRouterClient
from .pipeline import ExtractionPipeline
from .repository import EvidenceStore
from .report import write_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest PDF/PPTX/DOCX and extract traceable process evidence")
    parser.add_argument("documents", nargs="+", type=Path)
    parser.add_argument("--store", type=Path, default=Path("evidence-store"))
    parser.add_argument("--output", type=Path, default=Path("evidence-output"))
    parser.add_argument("--parse-only", action="store_true", help="Archive/parse offline, without model calls")
    parser.add_argument("--extraction-model", help="Override the Flash/task model for this run")
    parser.add_argument("--audit-model", help="Override the Pro/task model for this run")
    parser.add_argument("--batch-chars", type=int, default=16000)
    parser.add_argument("--max-calls", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if not args.parse_only:
            load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False, verbose=False, encoding="utf-8-sig")
        config = None if args.parse_only else LLMConfig.from_env()
        client = None if config is None else OpenRouterClient(config)
        store = EvidenceStore(args.store)
        pipeline = ExtractionPipeline(store, client, config, extraction_model=args.extraction_model,
            audit_model=args.audit_model, batch_chars=args.batch_chars, max_calls=args.max_calls)
        run_id = pipeline.run(args.documents, parse_only=args.parse_only)
        json_path, report_path = write_report(store, run_id, args.output)
        run = store.load().runs[run_id]
        print("Run: " + run_id + "; status: " + run.status + "; coverage: " + run.coverage)
        print("Evidence JSON: " + str(json_path))
        print("Evidence report: " + str(report_path))
        return 0 if run.status == "audited" or args.parse_only and run.status != "failed" else 2
    except Exception as exc:
        # Configuration errors mention variable names only. Other exception messages can contain secrets.
        from ..llm_config import ConfigurationError
        message = str(exc) if isinstance(exc, ConfigurationError) else type(exc).__name__
        print("Extraction failed: " + message, file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
