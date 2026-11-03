# Connecting an external client

[Русская версия](../ru/external-client.md) | [Documentation index](../index.md)

An external client can send requests to A Second Brain through the private HTTP
API. The client sends plain text, and the server decides whether to:

- answer a question;
- save a daily entry;
- execute a request on the server.

The current request API does not provide arbitrary filesystem access to the
vault. Use a separate remote-folder or synchronization mechanism when the
client needs to edit files.

## 1. Configure the server

Set the API token and listen address in the project's `.env`:

```dotenv
NETWORK_API_TOKEN=long-random-token
NETWORK_API_HOST=127.0.0.1
NETWORK_API_PORT=8765
```

The token must be random and must remain in `.env`. After changing the
configuration, render and enable the systemd units:

```bash
./scripts/install-systemd-user.sh --enable
```

The shared restart script also checks `NETWORK_API_TOKEN`: a non-empty value
starts the network service, while an empty value skips that step.

Check the service:

```bash
systemctl --user is-active a-second-brain-network.service
curl http://127.0.0.1:8765/healthz
```

The health check returns:

```json
{"status": "ok"}
```

## 2. Connect through an SSH tunnel

By default, the API listens only on `127.0.0.1`, so its port does not need to
be exposed to the internet. On the computer running the external client, run:

```bash
ssh -N -L 8765:127.0.0.1:8765 <user>@<server>
```

The external client can then use `http://127.0.0.1:8765` locally. Keep the SSH
session open while using the client.

## 3. Submit a request

Every request requires this authorization header:

```http
Authorization: Bearer <NETWORK_API_TOKEN>
```

Create a job:

```bash
curl -X POST http://127.0.0.1:8765/v1/requests \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "client-0001",
    "text": "What is the current status of project X?",
    "client": "external-client"
  }'
```

The response contains a `job_id`:

```json
{
  "job_id": "...",
  "request_id": "client-0001",
  "status": "queued"
}
```

`request_id` must be unique for one request. Resubmitting the same identifier
returns the existing job and does not execute the action again.

## 4. Read the result

Poll the job:

```bash
curl http://127.0.0.1:8765/v1/jobs/<job_id> \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

Possible states are `queued`, `running`, `completed`, and `failed`.

When the job is `completed`, `result.report` contains the textual response. If
the server created a final file, `result.artifacts` contains its name and a
`download_url`:

```bash
curl -L http://127.0.0.1:8765/v1/jobs/<job_id>/artifacts/artifact-0 \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -o result.bin
```

The server exposes only files explicitly declared as final artifacts by the
current workflow. Arbitrary vault paths cannot be requested through this API.

## 5. File API

The file API supports listing, reading, and updating existing files. Creating,
deleting, and renaming files are not supported.

```http
GET /v1/vault/tree?path=projects&depth=1&page_size=100
GET /v1/vault/file?path=projects/example.md
PUT /v1/vault/file?path=projects/example.md
POST /v1/vault/conflicts/<conflict_id>/resolve
```

The directory listing exposes only `user_content_roots` from
`vault-manifest.json`. Infrastructure directories, symlinks, special files,
absolute paths, backslashes, and paths containing `..` are rejected. `depth`
accepts 0 through 8, and `page_size` accepts 1 through 1000. Pass the opaque
`page_token` from the previous response to read the next page.

List a directory:

```bash
curl 'http://127.0.0.1:8765/v1/vault/tree?path=projects&depth=1' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

File reads return the original bytes, size, MIME type, modification time, and an
`ETag` based on the SHA-256 hash of the complete file. Text and binary files are
not decoded or transformed.

```bash
curl -D headers.txt \
  'http://127.0.0.1:8765/v1/vault/file?path=projects/example.md' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -o example.md
```

Update an existing file using the `ETag` from the response:

```bash
curl -X PUT \
  'http://127.0.0.1:8765/v1/vault/file?path=projects/example.md' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'If-Match: "<sha256>"' \
  -H 'Content-Type: application/octet-stream' \
  --data-binary @example.md
```

The server checks the `ETag` again immediately before the atomic write under the
vault lock.

If `If-Match` is missing, the server returns `428`. If the file changed, it
returns `409 Conflict` with `conflict_id`, `expected_etag`, `current_etag`, and
`proposed_etag`. The client must ask the user whether to replace or cancel:

```bash
curl -X POST \
  'http://127.0.0.1:8765/v1/vault/conflicts/<conflict_id>/resolve' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>' \
  -H 'Content-Type: application/json' \
  -d '{"decision":"replace"}'
```

Send `{"decision":"cancel"}` to cancel. A decision is one-time and bound to
the path, current fingerprint, and proposed bytes. If the file changes again,
the server creates a new conflict. Oversized files return `413`.

## 6. QMD search

Network search uses the local QMD index and always returns at most 20 results.
There is no client-configurable `limit` parameter.

```bash
curl 'http://127.0.0.1:8765/v1/vault/search?q=project%20NPD' \
  -H 'Authorization: Bearer <NETWORK_API_TOKEN>'
```

The response contains the canonical relative file path, directory, file name,
title, and snippet:

```json
{
  "query": "project NPD",
  "source": "qmd",
  "total": 2,
  "limited": false,
  "results": [
    {
      "path": "projects/npd/status.md",
      "directory": "projects/npd",
      "name": "status.md",
      "title": "NPD project status",
      "snippet": "Current project status..."
    }
  ]
}
```

When more than 20 results are available, the first 20 are returned with
`limited: true`. Search does not refresh the index or change the vault. Open a
result with `GET /v1/vault/file?path=...`.

## Errors

- `401` — the bearer token is missing or invalid;
- `400` — invalid JSON, empty `text`, or empty `request_id`;
- `403` — the path is outside the manifest user-content roots;
- `409` — the file changed or the conflict is stale;
- `411` — `Content-Length` is missing;
- `413` — the request body or file is too large;
- `428` — `If-Match` is missing for an update;
- `503` — QMD is unavailable or could not complete the search;
- `404` — unknown job or artifact;
- `failed` — the workflow failed; the job result contains the error.

If the service does not respond, check the SSH tunnel first, then inspect:

```bash
systemctl --user status a-second-brain-network.service --no-pager
journalctl --user -u a-second-brain-network.service -n 100 --no-pager
```
