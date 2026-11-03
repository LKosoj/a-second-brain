"""Small authenticated HTTP server for LacEditor requests."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

from d_brain.config import Settings
from d_brain.services.network_requests import (
    NetworkRequestError,
    NetworkRequestService,
)
from d_brain.services.vault_file_service import (
    MAX_FILE_BYTES,
    VaultFileConflict,
    VaultFileConflictResolved,
    VaultFileEditForbidden,
    VaultFileError,
    VaultFileForbidden,
    VaultFileListingChanged,
    VaultFileNotFound,
    VaultFileService,
)
from d_brain.services.vault_find import (
    FIND_QUERY_MAX_CHARS,
    NETWORK_FIND_LIMIT,
    FindUnavailable,
    normalize_query,
    search_in_vault,
)

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 256 * 1024


class NetworkServer:
    """Serve the LacEditor network API until interrupted."""

    def __init__(self, settings: Settings) -> None:
        if not settings.network_api_token:
            raise NetworkRequestError("NETWORK_API_TOKEN is not configured")
        self.settings = settings
        self.requests = NetworkRequestService(settings)
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer(
            (settings.network_api_host, settings.network_api_port), handler
        )

    def serve(self) -> None:
        logger.info(
            "Network API listening on %s:%s",
            self.settings.network_api_host,
            self.settings.network_api_port,
        )
        try:
            self.httpd.serve_forever()
        finally:
            self.requests.close()
            self.httpd.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        service = self.requests
        settings = self.settings

        class Handler(BaseHTTPRequestHandler):
            server_version = "a-second-brain-network/1"
            timeout = 60

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                if path == "/healthz":
                    self._send_json(HTTPStatus.OK, {"status": "ok"})
                    return
                if not self._authorized():
                    return
                if path == "/v1/vault/tree":
                    try:
                        query = self._query()
                        page = VaultFileService(settings.vault_path).list_tree(
                            query.get("path", [""])[0],
                            depth=int(query.get("depth", ["1"])[0]),
                            page_size=int(query.get("page_size", ["100"])[0]),
                            page_token=(query.get("page_token", [None])[0]),
                        )
                    except VaultFileListingChanged as exc:
                        self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
                        return
                    except (ValueError, VaultFileError) as exc:
                        self._send_file_error(exc)
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "path": page.path,
                            "depth": page.depth,
                            "entries": [
                                {
                                    "path": entry.path,
                                    "name": entry.name,
                                    "kind": entry.kind,
                                    "size": entry.size,
                                    "mtime_ns": entry.mtime_ns,
                                    "mime_type": entry.mime_type,
                                }
                                for entry in page.entries
                            ],
                            "next_page_token": page.next_page_token,
                            "listing_complete": page.listing_complete,
                        },
                    )
                    return
                if path == "/v1/vault/file":
                    try:
                        query = self._query()
                        file_path = query.get("path", [""])[0]
                        if not file_path:
                            raise VaultFileError("path is required")
                        file_result = VaultFileService(settings.vault_path).read_file(
                            file_path
                        )
                    except (ValueError, VaultFileError) as exc:
                        self._send_file_error(exc)
                        return
                    not_modified = _etag_matches(
                        self.headers.get("If-None-Match"), file_result.etag
                    )
                    self.send_response(
                        HTTPStatus.NOT_MODIFIED if not_modified else HTTPStatus.OK
                    )
                    self.send_header("Content-Type", file_result.mime_type)
                    self.send_header("Content-Length", str(file_result.size))
                    self.send_header("ETag", f'"{file_result.etag}"')
                    self.send_header("X-File-Modified-Ns", str(file_result.mtime_ns))
                    self.end_headers()
                    if not not_modified:
                        self.wfile.write(file_result.content)
                    return
                if path == "/v1/vault/search":
                    try:
                        query = self._query()
                        raw_query = query.get("q", [""])[0]
                        search_query = normalize_query(raw_query)
                        if not search_query:
                            raise VaultFileError("q is required")
                        if len(search_query) > FIND_QUERY_MAX_CHARS:
                            raise VaultFileError("q is too long")
                        search_result = search_in_vault(
                            settings.vault_path,
                            search_query,
                            limit=NETWORK_FIND_LIMIT,
                        )
                    except FindUnavailable:
                        self._send_json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "qmd search unavailable"},
                        )
                        return
                    except (ValueError, VaultFileError) as exc:
                        self._send_file_error(exc)
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "query": search_query,
                            "source": "qmd",
                            "total": search_result.total,
                            "limited": search_result.limited,
                            "results": [
                                {
                                    "path": hit.rel_path,
                                    "directory": str(PurePosixPath(hit.rel_path).parent)
                                    if PurePosixPath(hit.rel_path).parent
                                    != PurePosixPath(".")
                                    else "",
                                    "name": PurePosixPath(hit.rel_path).name,
                                    "title": hit.title,
                                    "snippet": hit.snippet,
                                }
                                for hit in search_result.hits
                            ],
                        },
                    )
                    return
                parts = [unquote(part) for part in path.split("/") if part]
                if len(parts) == 3 and parts[:2] == ["v1", "jobs"]:
                    try:
                        query = self._query()
                    except VaultFileError as exc:
                        self._send_file_error(exc)
                        return
                    raw_wait = query.get("wait", [None])[0]
                    if raw_wait is None:
                        job = service.get(parts[2])
                    else:
                        try:
                            wait = float(raw_wait)
                        except ValueError:
                            self._send_json(
                                HTTPStatus.BAD_REQUEST, {"error": "invalid wait"}
                            )
                            return
                        if wait < 0 or wait > 30:
                            self._send_json(
                                HTTPStatus.BAD_REQUEST,
                                {"error": "wait must be between 0 and 30"},
                            )
                            return
                        job = service.wait_for(parts[2], wait)
                    if job is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "job not found"},
                        )
                    else:
                        self._send_json(HTTPStatus.OK, _public_job(job))
                    return
                if (
                    len(parts) == 5
                    and parts[:2] == ["v1", "jobs"]
                    and parts[3] == "artifacts"
                ):
                    self._send_artifact(parts[2], parts[4])
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                path = urlsplit(self.path).path
                if path.startswith("/v1/vault/conflicts/") and path.endswith(
                    "/resolve"
                ):
                    conflict_id = (
                        path.removeprefix("/v1/vault/conflicts/")
                        .removesuffix("/resolve")
                        .strip("/")
                    )
                    try:
                        payload = self._read_json()
                        decision = payload.get("decision")
                        if not isinstance(decision, str):
                            raise VaultFileError("decision is required")
                        result = VaultFileService(settings.vault_path).resolve_conflict(
                            conflict_id, decision
                        )
                    except VaultFileConflict as exc:
                        self._send_json(
                            HTTPStatus.CONFLICT,
                            {"error": "conflict", **exc.record},
                        )
                        return
                    except VaultFileConflictResolved as exc:
                        self._send_json(
                            HTTPStatus.CONFLICT,
                            {"error": str(exc), "status": exc.status},
                        )
                        return
                    except VaultFileEditForbidden as exc:
                        self._send_json(
                            HTTPStatus.UNPROCESSABLE_ENTITY,
                            {"error": str(exc), "reason": exc.reason},
                        )
                        return
                    except (ValueError, VaultFileError) as exc:
                        self._send_file_error(exc)
                        return
                    if result is None:
                        self._send_json(HTTPStatus.OK, {"status": "cancelled"})
                    else:
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "status": "replaced",
                                "path": result.path,
                                "size": result.size,
                                "mtime_ns": result.mtime_ns,
                                "etag": result.etag,
                            },
                        )
                    return
                if path != "/v1/requests":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                try:
                    payload = self._read_json()
                    request_id = str(payload.get("request_id", "")).strip()
                    text = str(payload.get("text", ""))
                    client = str(payload.get("client", ""))
                    job = service.submit(request_id, text, client)
                except (NetworkRequestError, ValueError, json.JSONDecodeError) as exc:
                    status = (
                        HTTPStatus.CONFLICT
                        if str(exc) == "request_id reused with different text"
                        else HTTPStatus.BAD_REQUEST
                    )
                    self._send_json(status, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.ACCEPTED, _public_job(job))

            def do_PUT(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if urlsplit(self.path).path != "/v1/vault/file":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if_match = self.headers.get("If-Match")
                if if_match is None:
                    self._send_file_error(
                        VaultFileError("If-Match is required"),
                        status=HTTPStatus.PRECONDITION_REQUIRED,
                    )
                    return
                expected = if_match.strip().strip('"')
                if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
                    self._send_file_error(VaultFileError("invalid If-Match"))
                    return
                try:
                    query = self._query()
                    file_path = query.get("path", [""])[0]
                    if not file_path:
                        raise VaultFileError("path is required")
                    content = self._read_body(MAX_FILE_BYTES)
                    result = VaultFileService(settings.vault_path).update_file(
                        file_path, content, expected
                    )
                except VaultFileConflict as exc:
                    self._send_json(
                        HTTPStatus.CONFLICT, {"error": "conflict", **exc.record}
                    )
                    return
                except VaultFileEditForbidden as exc:
                    self._send_json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {"error": str(exc), "reason": exc.reason},
                    )
                    return
                except (ValueError, VaultFileError) as exc:
                    self._send_file_error(exc)
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "path": result.path,
                        "size": result.size,
                        "mtime_ns": result.mtime_ns,
                        "etag": result.etag,
                    },
                )

            def _authorized(self) -> bool:
                expected = f"Bearer {settings.network_api_token}"
                if self.headers.get("Authorization", "") != expected:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return False
                return True

            def _read_json(self) -> dict[str, object]:
                body = self._read_body(MAX_BODY_BYTES)
                value = json.loads(body.decode("utf-8"))
                if not isinstance(value, dict):
                    raise NetworkRequestError("JSON object is required")
                return value

            def _read_body(self, maximum: int) -> bytes:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise VaultFileError("Content-Length is required")
                length = int(raw_length)
                if length < 0 or length > maximum:
                    raise VaultFileError("request body is too large")
                chunks: list[bytes] = []
                remaining = length
                while remaining:
                    chunk = self.rfile.read(remaining)
                    if not chunk:
                        raise VaultFileError("incomplete request body")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                body = b"".join(chunks)
                expected_checksum = self.headers.get("X-Content-SHA256")
                if expected_checksum is not None and (
                    not re.fullmatch(r"[0-9a-fA-F]{64}", expected_checksum)
                    or hashlib.sha256(body).hexdigest().lower()
                    != expected_checksum.lower()
                ):
                    raise VaultFileError("body checksum mismatch")
                return body

            def _query(self) -> dict[str, list[str]]:
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise VaultFileError("duplicate query parameter")
                return query

            def _send_file_error(
                self,
                error: Exception,
                *,
                status: HTTPStatus | None = None,
            ) -> None:
                if status is None:
                    if isinstance(error, VaultFileForbidden):
                        status = HTTPStatus.FORBIDDEN
                    elif isinstance(error, VaultFileNotFound):
                        status = HTTPStatus.NOT_FOUND
                    elif "too large" in str(error):
                        status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                    elif "Content-Length" in str(error):
                        status = HTTPStatus.LENGTH_REQUIRED
                    else:
                        status = HTTPStatus.BAD_REQUEST
                self._send_json(status, {"error": str(error)})

            def _send_artifact(self, job_id: str, artifact_id: str) -> None:
                job = service.get(job_id)
                if job is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return
                result = job.get("result")
                raw_paths = (
                    result.get("artifact_paths", []) if isinstance(result, dict) else []
                )
                if not isinstance(raw_paths, list) or not artifact_id.startswith(
                    "artifact-"
                ):
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "artifact not found"},
                    )
                    return
                suffix = artifact_id.removeprefix("artifact-")
                try:
                    index = int(suffix)
                    raw_path = raw_paths[index]
                except (ValueError, IndexError, TypeError):
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "artifact not found"},
                    )
                    return
                if index < 0 or str(index) != suffix:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "artifact not found"},
                    )
                    return
                relative = str(raw_path)
                root = settings.vault_path.resolve()
                path = Path(relative).resolve()
                if root not in path.parents or not path.is_file():
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "artifact not found"},
                    )
                    return
                data = path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                )
                self.send_header("Content-Length", str(len(data)))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{_safe_filename(path.name)}"',
                )
                self.end_headers()
                self.wfile.write(data)

            def _send_json(self, status: HTTPStatus, value: dict[str, object]) -> None:
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                logger.info("network %s", format % args)

        return Handler


def _public_job(job: dict[str, object]) -> dict[str, object]:
    result = job.get("result")
    if isinstance(result, dict):
        result = _public_result(result, job["job_id"])
    public = {key: value for key, value in job.items() if key != "text"}
    if result is not None:
        public["result"] = result
    return public


def _safe_filename(name: str) -> str:
    return (
        name.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
    )


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    return any(value.strip().strip('"') == etag for value in header.split(","))


def _public_result(result: dict[str, object], job_id: object) -> dict[str, object]:
    public = dict(result)
    raw_paths = result.get("artifact_paths", [])
    artifacts: list[dict[str, object]] = []
    if isinstance(raw_paths, list):
        for index, raw_path in enumerate(raw_paths):
            relative = str(raw_path)
            path = Path(relative)
            artifacts.append(
                {
                    "id": f"artifact-{index}",
                    "name": path.name,
                    "mime_type": mimetypes.guess_type(path.name)[0]
                    or "application/octet-stream",
                    "download_url": f"/v1/jobs/{job_id}/artifacts/artifact-{index}",
                }
            )
    public["artifacts"] = artifacts
    public.pop("artifact_paths", None)
    public.pop("filed_artifact_path", None)
    return public


def run_network_server(settings: Settings) -> None:
    """Run the configured network server."""
    NetworkServer(settings).serve()


__all__ = ["NetworkServer", "run_network_server"]
