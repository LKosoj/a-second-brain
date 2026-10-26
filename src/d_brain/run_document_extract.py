"""CLI wrapper for one document extraction job."""

from __future__ import annotations

import argparse
import json
import resource
import sys
from pathlib import Path

from d_brain.services.document_extractors import extract_document_payload

# Measured peak is ~1.1 GB of address space (onnxruntime/magika), so 4 GiB
# leaves room for real documents and still bounds a decompression bomb.
DOCUMENT_EXTRACTION_MEMORY_LIMIT_BYTES = 4 * 1024**3


def _limit_memory() -> None:
    # Set here, not via preexec_fn in the parent: the parent runs this from a
    # worker thread, and preexec_fn is unsafe with threads (fork deadlocks).
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = DOCUMENT_EXTRACTION_MEMORY_LIMIT_BYTES
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass  # macOS rejects RLIMIT_AS; the parent timeout still bounds the job


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

    # The JVM of the mpp reader reserves far more address space than it uses
    # and dies under RLIMIT_AS; the parent timeout bounds it instead.
    if args.format != "mpp":
        _limit_memory()

    payload = extract_document_payload(
        Path(args.input),
        file_format=args.format,
        original_name=args.name,
    )
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
