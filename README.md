# Stowarr

Stowarr reconciles Radarr/Sonarr with the pool currently used by
qBittorrent. qBittorrent's `save_path` is authoritative.

Stowarr exposes three deliberately separate workflows:

- **Sync** performs a read-only bulk audit.
- **Reconcile** repairs library inconsistencies on qBittorrent's current pool and never changes the torrent save path.
- **Move** relocates torrent-owned data through the qBittorrent API to an explicitly selected pool, verifies it, and then reconciles the library.

Radarr movies are resolved through `downloadId → movieId → movieFile`. Sonarr
downloads are resolved through `downloadId → seriesId/episodeId → episodeFile`.
Incomplete Sonarr episode mappings are blocked rather than expanded to every
file in the series.

The first release intentionally defaults to dry-run. It supports the important
recovery case where qBittorrent has already moved a torrent between pools:

1. resolve the torrent hash through *Arr history;
2. match video files by unique size;
3. hash source library data and qBittorrent data;
4. create new library hardlinks on qBittorrent's pool;
5. update the *Arr root and pool tag;
6. unlink the old library names;
7. verify that Radarr/Sonarr reports the new library paths.

Reconcile treats qBittorrent data as read-only and does not pause, recheck, or
relocate the torrent. Move owns the pause → qBittorrent relocation → recheck →
resume sequence. Archive-backed Move execution remains locked until native
extraction, import confirmation, and cleanup form one recoverable transaction.

It refuses ambiguous matches, missing torrent data, paths outside configured
pools, and source files with additional unknown hardlinks. Cross-seed group
migration is deliberately not automatic yet.

## Setup

### GHCR images

The default Compose file references the public multi-architecture images
`ghcr.io/slashmad/stowarr-api:latest` and
`ghcr.io/slashmad/stowarr-web:latest`. Clone the repository, create only the
local files, and start the stack:

```bash
git clone https://github.com/slashmad/stowarr.git
cd stowarr
cp config/config.example.json config/config.json
cp .env.example .env
docker compose up -d
```

Set a unique `STOWARR_API_TOKEN`, host mount paths, and matching container paths
in `.env`. Service URLs and credentials can then be entered through Settings in
the WebUI. `config/config.json` and `.env` are local-only files excluded from
both Git and Docker build contexts.

For qBittorrent 5.2 or newer, set `QBITTORRENT_API_KEY`. Stowarr sends it as
`X-API-Key` and does not create a password session. For older qBittorrent
versions, leave the API key empty and set `QBITTORRENT_USERNAME` and
`QBITTORRENT_PASSWORD`; Stowarr then uses the official cookie-based WebAPI
login. When both methods are configured, the API key always takes precedence.

For local image development, run `docker compose build` before `up`.

All applications must use identical absolute media paths. Edit `compose.yaml`
if your existing containers use another shared network. The SQLite state is
kept in the local Docker volume `stowarr-state`, not on an NFS project mount.

## Safe first run

Keep `STOWARR_APPLY=false`, then use the torrent hash shown by qBittorrent:

Open `http://127.0.0.1:8787` on the Fedora host for the WebUI. It provides an
*Arr-style overview, the active pool routing schema, plan inspection with
structured blocking errors, and operation history. The UI does not expose
service credentials and does not bypass the `STOWARR_APPLY` safety setting.

The Compose stack contains two isolated services. `stowarr-web` serves static
assets and proxies `/api`; it has no secrets, state, or media mounts.
`stowarr-api` owns credentials, SQLite state, and media access. Its direct API
listener is bound to `127.0.0.1:8788` and requires a bearer token. Set a long,
random `STOWARR_API_TOKEN` in `.env` before exposing that listener beyond the
host.

## API

Read-only calls can be made directly with the bearer token:

```bash
curl -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  http://127.0.0.1:8788/api/plan/TORRENT_HASH

curl -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  http://127.0.0.1:8788/api/operations
```

Every destructive operation uses a mandatory two-step protocol. First request
a short-lived confirmation that is bound to the current plan and exact payload:

```bash
curl -X POST \
  -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"reconcile","torrentHash":"TORRENT_HASH","payload":{"auxiliaryFiles":[]}}' \
  http://127.0.0.1:8788/api/confirmations
```

Then send the returned token with the identical selection:

```bash
curl -X POST \
  -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"auxiliaryFiles":[],"confirmationToken":"RETURNED_TOKEN"}' \
  http://127.0.0.1:8788/api/reconcile/TORRENT_HASH
```

Tokens expire after ten minutes, are single-use, and become invalid if the plan
or payload changes. The same protocol applies to Move with `kind: "move"` and
`payload: {"targetPool":"p1"}`. With `STOWARR_APPLY=false`, an authorized
operation is still recorded only as `DRY_RUN`.

The current reconcile path talks directly to qBittorrent, Radarr and Sonarr.
Seerr, Prowlarr, Bazarr and Cleanuparr endpoints are recorded in the example
configuration for planned event and health integrations, but are not called yet.

## Safety boundary

This version does not silently react to category edits. Reconcile and Move are
explicit API operations protected by authenticated, single-use confirmations.
Automatic polling and coordinated cross-seed migration remain disabled.

## Media strategy matrix

Stowarr never assumes that every imported media file exists directly in the
torrent manifest. Unknown cases are blocked instead of guessed.

| Scenario | Detection | Destination operation | Required verification |
| --- | --- | --- | --- |
| Direct torrent media | A qBittorrent video matches the *Arr file by unique size | Hardlink from qBittorrent | Old library SHA-256 equals torrent SHA-256; an existing target must also match |
| Packed media already on the authoritative pool | The torrent contains archives and the imported library path is already on qBittorrent's pool | No media move | Preserve the imported media and validate paths |
| Packed media on the wrong pool | The torrent contains archives but no matching video and the library is on another pool | Re-extract into isolated staging on the authoritative pool | qBittorrent recheck and an archive integrity test must succeed; regenerated output must be hashed before import |
| Torrent sidecar | Non-video file is present in qBittorrent's manifest | Hardlink from qBittorrent | Existing targets must be identical |
| Library/plugin sidecar | File exists in the old library but not the torrent manifest | Optional verified copy | SHA-256 of source and temporary destination must match |
| Competing sidecars | Torrent and library files select the same destination name | Block automatic overwrite | Explicit conflict resolution is required |
| Unknown media origin | No matching torrent video and no archive set | Block | Manual investigation is required |
| Unknown hardlinks | A cross-pool source has additional hardlinks | Block | All link owners must be identified first |
| Existing different target | Destination exists with different content | Block | Never overwrite automatically |

For packed releases, the torrent infohash and qBittorrent recheck validate the
archive set. The extracted media is a disposable derived artifact and therefore
has its own SHA-256 verification chain; it cannot be hardlinked to archive data.
Cross-pool packed media is never copied from an old library as the authoritative
source. It must be regenerated from qBittorrent-owned archives by Stowarr's
extractor. The extractor uses the current Linux 7-Zip command-line tool behind a
restricted staging interface; Stowarr does not implement archive codecs.

### Native archive recovery design

Stowarr uses a dedicated staging root on each pool:

1. force-recheck the qBittorrent archive set;
2. create a per-operation staging directory on the authoritative pool;
3. test the complete archive set with 7-Zip without modifying qBittorrent data;
4. extract into an empty per-operation staging directory on the authoritative pool;
5. reject path traversal, links, missing volumes, encrypted input without a configured password, and ambiguous media matches;
6. inventory and hash every extracted output before allowing *Arr import;
7. remove stale derived files on the old pool only after the new import is verified;
8. remove the staging job without deleting qBittorrent-owned archive data.

RAR is one supported input format, not a universal extraction engine. Stowarr
uses 7-Zip because it reads RAR/RAR5, multipart RAR, ZIP, 7z, TAR, ISO and the
other common release formats through one maintained command-line interface.
