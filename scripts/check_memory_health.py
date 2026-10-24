"""Read-only offline memory diagnostics; no .env, service, model or network."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ember.health_check import check_memory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, help="One actual Ember instance directory, not the multi-instance parent")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--max-findings", type=int, default=200)
    args = parser.parse_args(argv)
    if args.max_findings < 1:
        parser.error("--max-findings must be positive")
    result = check_memory(args.data_dir, max_findings=args.max_findings)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Ember memory check:", result["status"], "(read-only)")
        print("Directory:", result["data_dir"])
        print("Counts:", json.dumps(result["statistics"], ensure_ascii=False))
        for item in result["findings"]:
            print(json.dumps(item, ensure_ascii=False))
        if result["findings_truncated"]:
            print("Findings truncated; increase --max-findings for the full list.")
        for limit in result["limits"]:
            print("Note:", limit)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
