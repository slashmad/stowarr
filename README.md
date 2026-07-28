# Stowarr

Stowarr is a self-hosted repair and storage-pool migration tool for
qBittorrent, Radarr, and Sonarr. It finds disagreements between torrent data
and *Arr libraries, repairs verified library mappings, and moves seeding data
between storage pools without treating filenames or paths as proof of content.

> [!WARNING]
> Stowarr is currently a beta release. It can move, link, copy, extract, and
> delete media after verification. Test the deployment against your own path,
> permission, hardlink, and backup strategy before relying on Write mode.

## Why Stowarr?

Multi-pool media installations can drift over time:

- qBittorrent seeds a release from one pool while Radarr or Sonarr points to
  another;
- a library import or manual move breaks the expected hardlink;
- a torrent category routes to the wrong download root;
- packed releases leave extracted media detached from their archive source; or
- duplicate and ambiguous *Arr history makes an automatic repair unsafe.

Stowarr treats qBittorrent's exact torrent identity and `save_path` as the
authoritative pool selection, then requires current *Arr mappings and
file-content verification before changing anything.

## What it does

Stowarr separates inspection, repair, and relocation into three workflows:

| Workflow | Purpose | Changes qBittorrent save path |
| --- | --- | --- |
| **Sync** | Audit hashes, categories, save paths, pools, and *Arr library paths | No |
| **Reconcile** | Repair a verified library path or hardlink on qBittorrent's current pool | No |
| **Move** | Relocate torrent data through qBittorrent and rebuild the verified library on another pool | Yes |

Radarr movies are resolved through `downloadId → movieId → movieFile`. Sonarr
downloads are resolved through `downloadId → seriesId/episodeId → episodeFile`.
Incomplete or ambiguous mappings are blocked instead of being expanded to
unrelated files.

The WebUI also provides:

- separate Move and Reconcile queue views backed by one global FIFO execution
  slot;
- live, minimizable progress with named verification stages;
- Safe assisted Sync repairs for unambiguous category fixes and freshly
  revalidated Reconcile candidates;
- persistent History with short public job IDs and execution logs;
- read-only recovery diagnosis after interrupted operations; and
- routing diagnostics for qBittorrent categories, *Arr download clients, tags,
  and root folders.

## Safety by design

- Fresh installations start in dry-run mode.
- Every mutation requires an explicit, single-use confirmation bound to the
  exact current plan and selected payload.
- Confirmation tokens expire after ten minutes and are bound to the exact plan
  and selected payload.
- Reconcile never pauses or relocates torrent data.
- Move owns the pause, qBittorrent relocation, recheck, and resume sequence.
- Existing files with different content are never overwritten automatically.
- Unknown hardlinks, ambiguous matches, and paths outside configured pools are
  blocked.
- Cross-seed group migration is not automatic.
- **INVARIANT-RECOVERY-001:** while recovery is required, no operation may
  mutate qBittorrent, Radarr, Sonarr, or media files. Read-only diagnosis,
  explicit recovery resolution, and local queue administration remain
  available.

External API mutations and filesystem writes pass through one fail-closed
mutation guard. Workflow-level checks provide early errors, while the guarded
client and filesystem boundaries prevent a missed entry-point check from
bypassing recovery.

Archive-backed cross-pool execution uses qBittorrent recheck, archive integrity
testing, isolated extraction, SHA-256 comparison, and a completed *Arr rescan
before any old derived media is removed.

If Stowarr or its host stops during a transaction, the operation is never
silently replayed or rolled back. Later writes remain paused until the operator
reviews the recorded stage, runs read-only diagnosis, inspects external state,
and explicitly acknowledges recovery.

## Requirements

- qBittorrent plus Radarr and/or Sonarr
- Docker Compose or another container platform
- the same absolute media paths visible to Stowarr, qBittorrent, and *Arr
- filesystem support for hardlinks when direct torrent media should be linked
  into a library
- one `stowarr-api` replica for each state database and media set

Stowarr supports multiple Sonarr root families below each pool, such as both
`series` and `anime`.

## Quick start

Public multi-architecture images are available for `linux/amd64` and
`linux/arm64`:

- `ghcr.io/slashmad/stowarr-api:latest`
- `ghcr.io/slashmad/stowarr-web:latest`

```bash
git clone https://github.com/slashmad/stowarr.git
cd stowarr
cp config/config.example.json config/config.json
cp .env.example .env
```

Edit `.env` and match the numeric identity to the user or shared media group
used by qBittorrent, Radarr, and Sonarr:

```dotenv
PUID=1000
PGID=1000
UMASK=002

# Leave blank to generate credentials on first startup.
STOWARR_API_TOKEN=
STOWARR_ADMIN_PASSWORD=
STOWARR_AUTH_METHOD=forms

# Start read-only.
STOWARR_APPLY=false
STOWARR_MEDIA_MOUNT_MODE=ro

POOL1_HOST_PATH=/path/on/host/pool1
POOL1_CONTAINER_PATH=/data/pool1
POOL2_HOST_PATH=/path/on/host/pool2
POOL2_CONTAINER_PATH=/data/pool2
```

The container paths must match the absolute media paths visible to
qBittorrent, Radarr, and Sonarr. Configure the same paths, pool categories,
tags, and *Arr roots in `config/config.json`.

Stowarr starts as root only long enough to make `/state` writable, then drops
to the numeric `PUID:PGID` before starting the API. It never recursively changes
ownership or permissions below the media mounts. `UMASK=002` provides the
group-writable files normally needed when qBittorrent, Radarr, Sonarr, and
Stowarr share a media group.

TrueNAS Apps normally run qBittorrent, Radarr, and Sonarr as `apps` (`568:568`).
For that deployment, use:

```dotenv
PUID=568
PGID=568
UMASK=002
```

Verify the configured identity against the actual user and group selected for
the installed TrueNAS apps instead of assuming that every installation uses
the defaults.

```bash
docker compose pull
docker compose up -d
```

On first startup, Stowarr creates the `admin` WebUI account and a separate API
key when the corresponding environment values are empty. Retrieve generated
credentials from the API log:

```bash
docker compose logs stowarr-api
```

The cleartext password and generated API key are printed only when created.
Only their secure persisted forms remain in the state volume. Reset a lost
password from the API container; omitting `--password` generates a new random
value:

```bash
docker compose exec stowarr-api stowarr reset-password
```

Open `http://127.0.0.1:8787`, sign in, and configure service connections under
**Settings**:

- qBittorrent URL and API key, or legacy username/password fallback;
- Radarr URL and API key;
- Sonarr URL and API key.

Only services with a URL are tested and saved. The dialog can always be closed;
full Sync, Move, Reconcile, and routing workflows become available after all three
services are connected. Secrets are stored by the API in the local `stowarr-state`
volume and are never returned to frontend JavaScript.

### qBittorrent authentication

For qBittorrent 5.2 or newer, use an API key. Stowarr sends it with the
`Authorization: Bearer` header and does not create a password session. API-key
authentication always takes precedence when both methods are configured.

For older qBittorrent versions, leave the API key empty and supply the WebUI
username and password. Stowarr then authenticates through
`/api/v2/auth/login` and uses qBittorrent's session cookie.

Connection credentials may be supplied through the onboarding UI. The matching
environment variables are also available for initial bootstrap:

```dotenv
QBITTORRENT_API_KEY=
QBITTORRENT_USERNAME=
QBITTORRENT_PASSWORD=
RADARR_API_KEY=
SONARR_API_KEY=
```

## Dry run and Write mode

The default deployment is intentionally non-destructive:

```dotenv
STOWARR_APPLY=false
STOWARR_MEDIA_MOUNT_MODE=ro
```

A confirmed operation is recorded as `DRY_RUN` and cannot modify media. To
enable Write mode, make the media mount writable and recreate the API
container:

```dotenv
STOWARR_APPLY=true
STOWARR_MEDIA_MOUNT_MODE=rw
```

```bash
docker compose up -d --force-recreate stowarr-api
```

Write mode does not bypass confirmations or safety checks. It can also be
toggled under **Settings → Execution mode** after Stowarr validates every
configured and discovered write destination. Container identity, bind mounts,
mount mode, listener ports, and environment-provided secrets remain deployment
settings that require a container recreate.

## Safe assisted Sync repair

After a Sync audit, **Plan safe fixes** classifies problems into:

1. category changes that can be proven from the exact torrent path and
   configured pool route;
2. root mismatches with a fresh, ready Reconcile plan; and
3. ambiguous items that remain manual.

Planning is read-only. Category changes and Reconcile queue insertion are
separate confirmed phases. Category repairs run first because they invalidate
the earlier audit; Stowarr then refreshes the audit and rebuilds every Reconcile
candidate before offering the next confirmation.

## Pool routing

Each pool defines:

- one or more qBittorrent download roots;
- a Radarr library root and category;
- a primary Sonarr library root and category;
- Radarr and Sonarr selection tags.

Stowarr also discovers every Sonarr root folder below each configured pool
prefix. Matching relative root families are preserved across pools, so a series
under `p1/anime` moves to `p3/anime`, while `p1/series` moves to `p3/series`.
The configured `sonarr_root` remains the fallback and must be a real Sonarr root.

The routing chain is:

```text
Radarr/Sonarr tag
        ↓ selects
*Arr qBittorrent download client
        ↓ sends category
qBittorrent category
        ↓ selects
qBittorrent save path and storage pool
```

Tags restrict which movie or series may use a download client. Tags do not set
the download path themselves. Stowarr's routing diagnostics compare the *Arr
download clients, categories, tags, root folders, and qBittorrent category save
paths.

## Move transaction

Move uses a complete, confirmation-bound manifest:

1. qBittorrent-owned files are identified from the torrent manifest.
2. Radarr/Sonarr-managed library files are resolved from download history.
3. For direct media, Stowarr proves that every current *Arr-managed file is the
   selected torrent release by hardlink identity or SHA-256. The torrent
   infohash proves historical item ownership but is not treated as proof that
   the item's current media file is still the same release. A replaced release
   blocks Move before qBittorrent or the filesystem is changed.
4. Untracked files below the torrent content directory and additional files in
   the current library directory are inventoried and hashed.
5. Every additional file must be assigned either **Move and verify** or
   **Delete after verification** in the WebUI.
6. qBittorrent is paused, relocates its tracked data, and completes a recheck.
7. Archive-derived media is regenerated in isolated staging when required. Each
   output must uniquely match the current *Arr-managed file by size and SHA-256.
8. Additional files selected for Move are copied and hash-verified.
9. Stowarr rebuilds the library, updates Radarr/Sonarr, waits for a successful
   rescan command, confirms the managed paths, and verifies selected sidecars.
10. Files selected for Delete and verified old sources are removed.
11. Empty old content and library directories are removed last. An unexpected
   remaining file fails the operation instead of being deleted recursively.

Torrents seeded directly from a configured movie or series library are listed
separately as **Library-seeded**. They are not assumed to be malformed downloads
and are never silently reassigned to a download category.

The Move confirmation fingerprint includes the destination pool, full plan,
and every additional-file action. A stale or altered plan cannot reuse an old
confirmation token.

## Queue and restart recovery

Move and Reconcile are displayed separately but share one global FIFO execution
slot. This prevents qBittorrent, *Arr, and filesystem mutations from
overlapping. A direct submission joins the end of the same FIFO whenever
another operation is active.

Every queued entry stores the reviewed selections and rebuilds its plan
immediately before execution. If qBittorrent, filesystem, or *Arr state no
longer matches the confirmation fingerprint, it fails before mutation and must
be reviewed again.

- Waiting jobs can be cancelled individually.
- Running jobs cannot be cancelled mid-transaction.
- **Clear** removes only terminal queue rows; waiting and running jobs remain.
- Operation History is independent from queue cleanup.

Waiting entries survive normal container restarts. If Stowarr restarts during
an operation, that entry becomes **Interrupted** and is never automatically
replayed. The Queue recovery panel pauses later writes, provides read-only
qBittorrent, *Arr, and filesystem diagnosis, and requires an explicit review
note before work resumes.

The WebUI **Guide** page summarizes every page and action button.

The sidebar reports live status for the Stowarr API, qBittorrent, Radarr, and
Sonarr. Green means connected, amber means not configured, and red means a
configured service is unavailable. It also shows **Write mode** or **Dry run**,
the running Stowarr version, and a link to the official
[slashmad/stowarr](https://github.com/slashmad/stowarr) repository. Status is
refreshed periodically and after connection settings are saved; credentials are
never returned with the status response.

## Service isolation

The stack contains two services:

- `stowarr-web` serves static assets and proxies `/api`. It has no media or
  state mounts. Browser requests require an authenticated administrator
  session stored in an `HttpOnly`, `SameSite=Strict` cookie and a CSRF header.
- `stowarr-api` owns service credentials, SQLite state, media access, and all
  filesystem operations.

The WebUI is bound to `127.0.0.1:8787` and uses its own admin authentication.
The direct API listener is bound to `127.0.0.1:8788` and independently requires
the API token. It accepts the *Arr-compatible `X-Api-Key` header as well as a
Bearer token. Do not expose either listener to another network without TLS.

Forms authentication is the secure default. `STOWARR_AUTH_METHOD=external`
trusts the username supplied by `STOWARR_EXTERNAL_USER_HEADER` (default
`X-Forwarded-User`) and is intended only for Authelia, Authentik, or another
authentication proxy. In external mode, the Stowarr WebUI port must not be
reachable by clients through any path that bypasses that proxy. The proxy must
replace, rather than merely preserve, the trusted username header supplied by
the client.

Settings shows active WebUI sessions and a persistent, bounded security event
log. Session tokens are stored only as hashes in the same SQLite state database
as the rest of Stowarr, so an authenticated browser remains signed in across
container and image updates when `/state` is persistent. Sessions expire after
12 hours. Password changes and the **Sign out all sessions** action invalidate
every existing session.

The Security page shows the latest 100 events in a fixed-height scrolling
table. Stowarr retains at most 500 security events in SQLite, preventing
successful-login audit entries from growing the database without a limit.

## API

API requests can use the same `X-Api-Key` convention as Radarr, Sonarr, and
Prowlarr:

```bash
curl -H "X-Api-Key: $STOWARR_API_TOKEN" \
  http://127.0.0.1:8788/api/operations
```

Bearer authentication remains supported:

```bash
curl -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  http://127.0.0.1:8788/api/plan/TORRENT_HASH

curl -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  http://127.0.0.1:8788/api/operations
```

Destructive operations use a mandatory two-step protocol. First issue a
confirmation bound to the current plan and payload:

```bash
curl -X POST \
  -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"reconcile","torrentHash":"TORRENT_HASH","payload":{"auxiliaryFiles":[]}}' \
  http://127.0.0.1:8788/api/confirmations
```

Then submit the returned token with the identical selection:

```bash
curl -X POST \
  -H "Authorization: Bearer $STOWARR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"auxiliaryFiles":[],"confirmationToken":"RETURNED_TOKEN"}' \
  http://127.0.0.1:8788/api/reconcile/TORRENT_HASH
```

Move uses the same protocol with `kind: "move"` and a payload such as
`{"targetPool":"p1"}`.

## Media strategy

Stowarr does not assume that every imported media file exists directly in the
torrent manifest.

| Scenario | Destination operation | Verification |
| --- | --- | --- |
| Direct torrent media | Hardlink from qBittorrent data | Source, torrent, and existing target hashes must agree |
| Current *Arr file replaced by another release | Block before mutation | Import the intended torrent release or select the torrent matching the current file, then recheck identity |
| Torrent seeded directly from a library | Classify as Library-seeded | Preserve its path until an explicit, verified migration is requested |
| Torrent sidecar | Hardlink from qBittorrent data | Existing targets must be identical |
| Library or plugin sidecar | Optional verified copy | Source and temporary destination hashes must agree |
| Packed media already on the authoritative pool | Keep imported media | Validate qBittorrent and *Arr paths |
| Packed media on another pool | Native verified re-extraction | Archive recheck, integrity test, isolated extraction, SHA-256 match, and completed *Arr rescan |
| Competing sidecars | Block automatic overwrite | Explicit conflict resolution required |
| Unknown media origin | Block | Manual investigation required |
| Unknown additional hardlinks | Block | Every link owner must be identified |

For packed releases, the torrent infohash and qBittorrent recheck validate the
archive set. Extracted media is a derived artifact and requires its own
verification chain; it cannot be hardlinked to archive data.

The extraction foundation uses the current Linux 7-Zip command-line tool behind
a restricted staging interface. It recognizes common RAR/RAR5, multipart RAR,
ZIP, 7z, TAR, and ISO layouts. It discovers independent archive sets, publishes
only uniquely verified *Arr-managed media, and rolls back newly published files
if a later transaction step fails.

### Unpackerr coexistence

Stowarr must be the only archive extractor for downloads that it can Move or
Reconcile. Do not let Unpackerr automatically extract the same Radarr/Sonarr
items or watch any download root managed by Stowarr. Two extractors operating on
the same release can duplicate heavy disk I/O, race while files are changing,
leave `_unpackerred` output behind, and invalidate Stowarr's verification plan.

For a deployment where Stowarr manages all configured Radarr and Sonarr pools:

- disable or remove Unpackerr's `[[radarr]]` and `[[sonarr]]` integrations;
- disable or remove every Unpackerr `[[folder]]` entry whose `path` is a
  Stowarr `download_root`;
- restart Unpackerr after changing its configuration; and
- keep the same media paths mounted read/write only where another, explicitly
  separate Unpackerr workload still requires them.

Unpackerr may remain enabled for applications or paths outside Stowarr, but the
two tools must have mutually exclusive scopes. A delay, `parallel = 1`, or
`move_back = false` does not prevent the race and is not an isolation boundary.

Stowarr recognizes existing `_unpackerred` output as a derived artifact. It
removes that output only after the extracted media hash matches the published
library file and the complete Move transaction has passed verification. Do not
manually delete old derived output while a Move is running.

## Development

Install repository-local development and analysis tools:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
npm ci
scripts/bootstrap-analysis-tools.sh
```

The bootstrap script installs checksum-pinned ShellCheck, actionlint, and
Hadolint binaries under the ignored `.tools/` directory. Run the complete
repository check:

```bash
scripts/check.sh
```

It covers Python tests, Ruff, MyPy, Bandit, JavaScript, HTML, CSS, shell
scripts, GitHub Actions, Dockerfiles, Compose and JSON syntax, dependency
advisories, and secret scanning where applicable.

Build local images:

```bash
docker compose build
```

Run the test suite:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Pull requests run analysis, tests, Compose validation, container builds, and
secret scanning. Merges to `main` publish both multi-architecture GHCR images
with provenance and SBOM attestations.

## Versioning

Stowarr follows Semantic Versioning. Prereleases use tags such as
`v1.0.0-beta.4`, while the WebUI and API expose the corresponding product
version `1.0.0-beta.4`. Python package metadata uses the PEP 440 equivalent
`1.0.0b4`. Tagged builds are published to GHCR alongside commit-SHA images;
`latest` continues to track `main`.

## Project status

Stowarr is an early release. Keep **Dry run** enabled until plans have been
reviewed against your own qBittorrent, Radarr, Sonarr, filesystem, and hardlink
layout.

See [SECURITY.md](SECURITY.md) for vulnerability reporting.

## License and attribution

Copyright 2026 [slashmad](https://github.com/slashmad).

Stowarr is licensed under the [Apache License 2.0](LICENSE). Redistributions
must preserve the license, copyright, and attribution notices required by that
license, including the attribution in [NOTICE](NOTICE).
