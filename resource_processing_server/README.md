# Resource Processing Server

Independent service for processing object-storage backed resources before they are upserted into the search server.

Responsibilities:

- Accept resource manifests containing `storage_profile_id + object_key` file references.
- Validate and download source files from object storage.
- Reuse provided previews when valid, or generate previews with the shared `Tools/ResourceProcessor` preview pipeline.
- Reuse provided descriptions when present, or generate descriptions with the existing `ResourceProcessor.description` provider system.
- Accept both single-resource jobs and batch job submission; internally each resource remains an independent job.
- Scope job query/retry and processed-resource deletion by `X-Client-Id`.
- Submit processed records to the search server through `POST /resources/upsert`.
- Store the latest successful processing snapshot per client resource so SearchServer upserts can be replayed after failures or rebuilds.

The service is intentionally deployed separately from the search server. It does not write the search database directly.

The processing service reads its deployment configuration from
`resource_processing_server/.env` and secrets from
`resource_processing_server/.env.local`. Set `RP_LLM_PROVIDER` /
`RP_LLM_MODEL` in the former and the selected provider API key in the latter.
`Tools/.env` remains public client-tool configuration and is copied with the
code. Client-only secrets stay in `Client/.env.local`; it is not a server
deployment file.

Snapshots are stored in `RP_SNAPSHOT_DB_PATH` and contain replayable metadata only: source object refs, selected preview refs, selected description, client metadata, optional `package_object`, and SearchServer upsert status. They do not store binaries or embeddings.

Replay endpoints:

- `POST /processed-resource-snapshots/{client_resource_id}/replay`
- `POST /processed-resource-snapshots/replay`

Description generation can be coalesced by count and wait time with:

- `RP_DESCRIPTION_BATCH_ENABLED`
- `RP_DESCRIPTION_BATCH_MIN_SIZE`
- `RP_DESCRIPTION_BATCH_MAX_SIZE`
- `RP_DESCRIPTION_BATCH_MAX_WAIT_SECONDS`

Run locally:

```powershell
uvicorn resource_processing_server.app.main:app --reload --port 8100
```

Run as an independent Docker service:

```powershell
cd resource_processing_server
docker compose up -d --build
```

Start `preview-renderer` separately when you want Docker-owned preview
generation. The renderer is independent: callers generate a temporary read URL
for the source object and pass it as `source_object_url`; the renderer only
downloads that URL and returns preview bytes/zip. The processing server calls it
through `RP_PREVIEW_RENDERER_URL`, then uploads returned preview files to object
storage before description generation. Windows clients can call the same service
at `http://localhost:8200` after uploading source objects and save/upload the
returned preview files themselves.

Both services require API key auth outside debug mode. Configure client-bound
keys on the receiving service, and pass the renderer key from the processing
server when it calls preview-renderer:

```env
RP_CLIENT_API_KEYS=resource-crawler:<processing-api-key>
PR_CLIENT_API_KEYS=resource-crawler:<renderer-api-key>
RP_PREVIEW_RENDERER_API_KEY=<renderer-api-key>
PR_ALLOWED_SOURCE_URL_HOSTS=assets.example.com,*.cdn.example.com
```

`PR_ALLOWED_SOURCE_URL_HOSTS` is required for renderer downloads. Redirects are
rejected; callers should pass a final signed object/CDN URL. Processing-server
work directories are removed after each job by default. Set `RP_KEEP_WORK_DIR=1`
only for debugging failed jobs.

External Linux tools used by the processing images can be pre-downloaded into
`resource_processing_server/docker/vendor/apt`. The Dockerfile installs those
vendored `.deb` files first, so a deployment rebuild does not need to download
ffmpeg, Chromium, Node, fonts, or optional Blender again.

When SearchServer is on another machine, set:

```env
RP_SEARCH_SERVER_URL=http://search-server-host:8000
```

Operational commands are documented in `resource_processing_server/OPERATIONS_GUIDE.md`.
