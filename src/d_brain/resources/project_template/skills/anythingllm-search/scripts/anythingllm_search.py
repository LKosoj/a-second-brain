#!/usr/bin/env python3
"""Search the Data & AI knowledge base in AnythingLLM (read-only RAG retrieval).

This script ONLY reads from a single AnythingLLM workspace. It never uploads,
embeds, edits or deletes anything. Use it as an extra knowledge source when
synthesizing material on Data & AI topics.

Usage:
    python anythingllm_search.py "feature stores best practices"
    python anythingllm_search.py "RAG evaluation metrics" --top 8
    python anythingllm_search.py "vector db tradeoffs" --threshold 0.6
    python anythingllm_search.py "what is GraphRAG" --mode query
    python anythingllm_search.py "llm fine-tuning" --json
    python anythingllm_search.py --check

Modes:
    search (default)  POST /vector-search -> raw relevant chunks + sources.
                      No LLM generation, no service token cost. Agent synthesizes.
    query             POST /chat (mode=query) -> answer grounded strictly in the
                      workspace documents, with citations. Spends service LLM tokens.

Config (read from the repo-root .env only; environment variables are ignored):
    ANYTHINGLLM_BASE_URL    e.g. https://anythingllm.example.com  (no /api suffix)
    ANYTHINGLLM_API_KEY     developer API key (Bearer token)
    ANYTHINGLLM_WORKSPACE   workspace slug scoped to Data & AI
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

TIMEOUT_SEARCH = 30
TIMEOUT_CHAT = 90
REQUIRED_CONFIG = (
    "ANYTHINGLLM_BASE_URL",
    "ANYTHINGLLM_API_KEY",
    "ANYTHINGLLM_WORKSPACE",
)


def is_repo_root(path: Path) -> bool:
    git_path = path / ".git"
    return git_path.is_file() or (git_path / "HEAD").is_file()


def repo_env_file(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        if is_repo_root(parent):
            return parent / ".env"
    sys.exit(f"Error: cannot locate repo root from {here}.")


def read_env_file(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        sys.exit(
            f"Error: missing config file: {env_file}.\n"
            "Create it from .env.example and set: "
            + ", ".join(REQUIRED_CONFIG)
            + "."
        )

    values: dict[str, str] = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def config() -> tuple[str, str, str]:
    env_file = repo_env_file()
    values = read_env_file(env_file)
    base = values.get("ANYTHINGLLM_BASE_URL", "").rstrip("/")
    key = values.get("ANYTHINGLLM_API_KEY", "")
    workspace = values.get("ANYTHINGLLM_WORKSPACE", "")
    missing = [
        name
        for name, val in (
            ("ANYTHINGLLM_BASE_URL", base),
            ("ANYTHINGLLM_API_KEY", key),
            ("ANYTHINGLLM_WORKSPACE", workspace),
        )
        if not val
    ]
    if missing:
        sys.exit(
            f"Error: missing or empty required config key(s) in {env_file}: "
            + ", ".join(missing)
            + "."
        )
    return base, key, workspace


def call(method: str, url: str, key: str, body: dict | None, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 401 or exc.code == 403:
            sys.exit(f"Error: auth failed ({exc.code}). Check ANYTHINGLLM_API_KEY.")
        if exc.code == 404:
            sys.exit(
                f"Error: not found ({exc.code}). Check ANYTHINGLLM_BASE_URL and "
                f"ANYTHINGLLM_WORKSPACE.\n{detail}"
            )
        sys.exit(f"Error: HTTP {exc.code} from AnythingLLM.\n{detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"Error: cannot reach AnythingLLM at {url}: {exc.reason}")


def check(base: str, key: str, workspace: str) -> None:
    auth = call("GET", f"{base}/api/v1/auth", key, None, TIMEOUT_SEARCH)
    if not auth.get("authenticated"):
        sys.exit("Error: API key rejected by instance.")
    ws = call("GET", f"{base}/api/v1/workspace/{workspace}", key, None, TIMEOUT_SEARCH)
    found = ws.get("workspace")
    if not found:
        sys.exit(
            f"Error: workspace '{workspace}' not found. "
            "Check ANYTHINGLLM_WORKSPACE (use the slug, not the display name)."
        )
    name = found[0].get("name") if isinstance(found, list) else found.get("name")
    print(f"OK: connected. Workspace '{workspace}' ({name}) is reachable.")


def do_search(base: str, key: str, workspace: str, query: str, top: int,
              threshold: float | None) -> dict:
    body: dict = {"query": query, "topN": top}
    if threshold is not None:
        body["scoreThreshold"] = threshold
    url = f"{base}/api/v1/workspace/{workspace}/vector-search"
    return call("POST", url, key, body, TIMEOUT_SEARCH)


def do_query(base: str, key: str, workspace: str, query: str) -> dict:
    body = {"message": query, "mode": "query"}
    url = f"{base}/api/v1/workspace/{workspace}/chat"
    return call("POST", url, key, body, TIMEOUT_CHAT)


def print_search(result: dict, query: str) -> None:
    results = result.get("results") or []
    if not results:
        print(f"No matching chunks for: {query!r}")
        print("Try a broader query or lower --threshold.")
        return
    print(f"{len(results)} chunk(s) for: {query!r}\n")
    for i, r in enumerate(results, 1):
        meta = r.get("metadata") or {}
        title = (
            meta.get("title") or meta.get("docSource") or r.get("id") or "(untitled)"
        )
        score = r.get("score")
        url = meta.get("url")
        head = f"[{i}] {title}"
        if score is not None:
            head += f"  (score {score:.3f})"
        print(head)
        if url and url != "no-url":
            print(f"    source: {url}")
        text = (r.get("text") or "").strip()
        if text:
            print("    " + text.replace("\n", "\n    "))
        print()


def print_query(result: dict, query: str) -> None:
    if result.get("error"):
        sys.exit(f"Error from AnythingLLM: {result['error']}")
    answer = (result.get("textResponse") or "").strip()
    print(f"Answer for: {query!r}\n")
    print(answer or "(empty answer — no relevant documents in the workspace)")
    sources = result.get("sources") or []
    if sources:
        print("\nSources:")
        seen = set()
        for s in sources:
            title = s.get("title") or "(untitled)"
            if title in seen:
                continue
            seen.add(title)
            print(f"  - {title}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Data & AI knowledge search against AnythingLLM."
    )
    parser.add_argument("query", nargs="?", help="search query")
    parser.add_argument("--mode", choices=["search", "query"], default="search",
                        help="search = raw chunks (default); query = grounded answer")
    parser.add_argument("--top", type=int, default=6,
                        help="max chunks to return in search mode (default 6)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="min similarity score (0-1); omit to use workspace default",
    )
    parser.add_argument("--json", action="store_true", help="print raw JSON response")
    parser.add_argument("--check", action="store_true",
                        help="verify connectivity, auth and workspace, then exit")
    args = parser.parse_args()

    base, key, workspace = config()

    if args.check:
        check(base, key, workspace)
        return

    if not args.query:
        parser.error("query is required (or use --check)")

    if args.mode == "query":
        result = do_query(base, key, workspace, args.query)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_query(result, args.query)
    else:
        result = do_search(base, key, workspace, args.query, args.top, args.threshold)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_search(result, args.query)


if __name__ == "__main__":
    main()
