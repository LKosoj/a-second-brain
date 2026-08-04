"""CLI wrapper for one document extraction job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from d_brain.services.document_extractors import extract_document_payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract one saved document to text")
    parser.add_argument(
        "--input",
        required=True,
        help="Absolute path to the saved file",
    )
    parser.add_argument("--format", required=True, help="Canonical document format")
    parser.add_argument("--name", required=True, help="Original filename")
    args = parser.parse_args()

    payload = extract_document_payload(
        Path(args.input),
        file_format=args.format,
        original_name=args.name,
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
