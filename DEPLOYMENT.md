# Deployment

Updated: 2026-11-17

Complete the local setup in [README.md](README.md) first. Keep configuration, passwords, employee data, photos, and generated files outside the published source. The local Python server uses its own SQLite database. The hosted runtime can use native SQL Server tables with its original Cloudflare SQLite database retained as a mirror and recovery queue. Deploying code does not migrate data or synchronize a separate local database.

## HTTPS and environment

Use a trusted HTTPS reverse proxy for shared access. Configure these variables in the server process:

| Variable | Purpose |
| --- | --- |
| `PARTY_HOST`, `PARTY_PORT` | Listen address and port; defaults are `0.0.0.0` and `8080` |
| `PARTY_PUBLIC_ORIGIN` | Exact public HTTPS origin, such as `https://eventdesk.example.invalid` |
| `PARTY_COOKIE_SECURE` | Set to `1` when serving HTTPS |
| `PARTY_OPEN_STAFF` | Set to `1` only when public staff access is intended |
| `PARTY_CONFIG` | Private config path; default `data/config.json` |
| `PARTY_DATABASE` | SQLite path; default `data/party.sqlite3` |
| `PARTY_EMPLOYEE_JSON` | Initial employee JSON; default `data/employees.json` |
| `PARTY_CERT_FILE`, `PARTY_KEY_FILE` | Optional paired certificate and private key for direct TLS |

Native Python runs read operating-system environment variables, not `.env` files.

For trusted-device LAN testing, the certificate helper creates a local test CA and server certificate:

```powershell
.venv\Scripts\python.exe scripts\generate_lan_certificate.py --ip YOUR-LAN-IP
```

Replace `YOUR-LAN-IP` with the server's address. Trust `data/eventdesk-test-ca.cer` on the test devices. The app detects its default server certificate and key under `data/`. Keep private keys private. Mobile camera streaming needs trusted HTTPS; ordinary LAN HTTP cannot enable it.

## Docker

The image runs the same Python app with a persistent `/app/data` directory. Docker Compose exposes it only on `127.0.0.1:18080`; a local HTTPS reverse proxy must forward traffic there.

```powershell
Copy-Item .env.example .env
docker compose build
docker compose run --rm eventdesk python setup.py
docker compose up -d
```

Before running Compose, edit `.env` to set the proxy's actual HTTPS origin. Compose reads that file. Its supplied configuration enables secure cookies and public staff access; review those choices for your event. Setup initializes the named data volume. Back up that volume separately from the source repository.

## Hosted SQL backend

The hosted path reuses the existing authenticated photo API and tunnel:

```text
Browser -> Pages -> Worker / Durable Object coordinator
                     -> protected gateway -> tunnel/VPC -> photo API :8765
                                                          -> event backend :8766 -> SQL Server
                     -> retained SQLite mirror and durable recovery queue
```

Both Windows listeners bind only to loopback. The C# photo API proxies only the fixed `/v1/event/request` and `/v1/event/snapshot` routes to the Python event backend. It does not expose arbitrary SQL or arbitrary network destinations. `EventDeskEventBackend` supervises Python and loads the existing protected SQL settings; `EventDeskPhotoApi` remains responsible for photos and authenticated proxying. `Cloudflared` carries the existing tunnel.

### Prepare the host and package

SQL Server 2014 and compatibility level 120 or newer are supported. Large lists use parameterized XML rather than requiring `OPENJSON`. The existing protected photo configuration retains its `Employee_db` database guard. Event tables use the separately reviewed `eventdesk` or `dbo` schema consistently across DDL, migration, and runtime.

Install the matching 64-bit Microsoft ODBC Driver 18 or 17 for SQL Server. The driver is free, but its license acceptance and installation are separate operator steps; the backend installer does neither. The legacy `SQL Server` ODBC driver is insufficient. The current package validator requires Windows x64 Python 3.14 dependencies; use the same Python ABI as the backend host, rather than copying a development virtual environment.

Create a protected staging parent, then choose fresh dependency and package directories:

```powershell
.\photo_api\build.ps1
py -3.14 -m pip install --only-binary=:all: --no-compile --target C:\EventDeskStaging\release-deps -r requirements-sql.txt
py -3.14 scripts\build_sql_backend_package.py --dependencies C:\EventDeskStaging\release-deps --binaries photo_api\bin --output C:\EventDeskStaging\release-package
```

The builder uses an explicit source/dependency allowlist, includes the two service executables, refuses an existing output directory, and emits `manifest_sha256`. Retain that manifest hash through a trusted channel. Credentials, options, and private `data/` files must stay outside the package.

Prepare a protected options JSON separately with `PublicOrigin`, `AppConfig`, and `Schema`. Use your exact HTTPS origin, preserve the existing signing secret and password hashes in `AppConfig`, and choose `eventdesk` or `dbo`. These values must match the Worker configuration and migration target.

Review the host guards in both [install_event_backend.ps1](photo_api/install_event_backend.ps1) and [migrate_event_backend.ps1](photo_api/migrate_event_backend.ps1). Replace `PHOTO-API-HOST` with the same approved hostname in both scripts before installation or migration. Keep both guards enabled. Supply `-SourceDirectory`, `-ExpectedManifestSha256`, `-PythonBase`, and `-OptionsPath` to the installer. Its default is a validation/plan only. Explicit `-Install` requires elevation, preserves the photo configuration, backs up its binary, and registers the backend without starting it. It does not migrate SQL data and rejects an existing backend installation; use a reviewed update procedure for later releases.

The installed files live under `ProgramData\EventDeskPhotoApi\event-backend`; options and runtime permissions are restricted by the installer. The event service and event migration read SQL credentials from protected configuration, not environment variables, command arguments, Cloudflare bindings, or source control. The separate photo importer uses the process environment as described below.

### Migrate and cut over

1. Rehearse native SQL, authenticated proxying, and outage recovery in an isolated database. Prepare empty event tables and protocol tables after checking for schema/name collisions. [create_event_database.sql](photo_api/sql/create_event_database.sql) defaults to `eventdesk`; use `eventdesk_storage.schema.schema_statements` to generate DDL for another reviewed schema. Schema creation and foreign keys need narrow DDL/`REFERENCES` permissions; runtime use needs normal table read/write permissions, not `db_owner`.
2. Stage the Worker with explicit `EVENT_STORAGE=sqlite`. Preserve the existing Durable Object namespace and instance, bindings, and assets. Include `event_worker_proxy.mjs` with the private gateway deployment.
3. Set `EVENT_MAINTENANCE=1` to freeze `/api` traffic. Take a fresh consistent export of the authoritative hosted event database, preserving all event tables, sessions, QR tokens, histories, images, and signing configuration. Retain the original backup; a separate local SQLite file is not a replacement for this export.
4. Validate the frozen export before writing SQL:

```powershell
.\photo_api\migrate_event_backend.ps1 -SourcePath C:\EventDeskStaging\event-export.json -ExpectedSourceSha256 REVIEWED_FILE_SHA256
```

Replace the path and hash with your private export and its reviewed SHA-256. The wrapper defaults to dry run and does not load SQL credentials. `ExpectedSourceSha256` pins the exact file bytes; the reported canonical `SnapshotSha256` represents the parsed event content and is a different hash. Add `-Commit` only on the approved host in an elevated session, with `EventDeskEventBackend` stopped. It reads installed protected settings and passes credentials through private standard input.

The underlying CLI is `scripts/migrate_sql_event.py --source <export> --schema <schema> --dry-run`; direct commit requires `--commit --connection-stdin`. The importer preserves identities/relationships, compares table counts and hashes, and commits an import receipt with the data. Retrying the identical snapshot reconciles a lost acknowledgment. Do not erase nonempty targets or bypass a mismatch.

5. Start the backend after verified import. Verify tokens, sessions, attendance, winners, images, and foreign keys. Seed/validate replica metadata against that same SQL snapshot and revision before enabling fallback.
6. Set `EVENT_STORAGE=sql`, clear maintenance, and verify SQL/synced responses and queued-check-in recovery. Preserve the existing Durable Object; do not rerun its original bootstrap.

See [SQL_FAILOVER.md](cloudflare/SQL_FAILOVER.md) for protocol details, safety boundaries, and recovery checks.

### Outage and recovery

`EVENT_STORAGE` defaults to `sql`; explicit `sqlite` selects the retained original handler path for controlled migration or rollback. `EVENT_MAINTENANCE=1` rejects hosted `/api` traffic before database work. These are Worker bindings, separate from the local Python configuration.

Healthy SQL mutations commit their operation receipt and business changes together; revisioned changes update the Cloudflare copy. The browser reuses a stable operation UUID for retries. An uncertain write must be reconciled against its receipt or cancellation tombstone; never retry a draw under a new UUID to guess whether it succeeded.

If the private API is unavailable, a verified last-good mirror can serve reads and atomically save a check-in plus its queue entry. Such check-ins return HTTP 202 and display **Saved offline**; they are pending until SQL accepts them. Draws, resets, replacements, and other conflicting mutations halt. Authentication and malformed-protocol failures fail closed. Photos may show a placeholder.

Recovery is request-driven, not a background timer: incoming traffic reconciles uncertain receipts, replays queued check-ins in order with original timestamps, and applies committed SQL revisions. It does not replace SQL with a stale SQLite file. Queued-session proofs have a seven-day recovery window; rejected/conflicting entries need staff review. Inspect pending work before switching storage modes or restoring backups.

This fallback still requires Cloudflare reachability, a verified mirror, and sufficient Workers/Durable Object/KV quota. If a durable intent cannot be saved, unsafe mutations halt even when SQL is reachable. No paid upgrade is required or enabled by this source; capacity and Free quota must still be monitored and rehearsed.

## Employee photos

The app works without photos. The optional path is:

```text
Browser -> app photo endpoint -> protected edge gateway
        -> Cloudflare tunnel/VPC binding -> Windows loopback API -> SQL Server
```

Photos remain in `Employee_db.dbo.EventDeskEmployeePhoto`, separate from event tables and the mirror protocol. Photo requests require canonical seven-digit numeric employee IDs; the synthetic alphanumeric sample records do not have matching photos. The application works without a photo for every employee.

### Prepare and import

Build on Windows with the .NET Framework compiler available:

```powershell
.\photo_api\build.ps1
.venv\Scripts\python.exe photo_api\prepare_photos.py --photos path\to\photos --database data\party.sqlite3 --output data\photo-import\batch
```

Use a new or empty output folder. Preparation reads originals without changing them, creates bounded JPEGs with metadata removed, and writes a manifest and report. Review conflicts and missing matches before importing. `--prefer-main-folder` selects an unambiguous original outside a JPEG subfolder when copies differ.

Have a database administrator run [create_employee_photo_table.sql](photo_api/sql/create_employee_photo_table.sql). Give the importer the required table permissions and the runtime API read access. Supply an encrypted SQL connection string through the process environment variable `EVENTDESK_IMPORT_CONNECTION`; do not put credentials in command arguments, source files, or Git.

```powershell
.\photo_api\bin\ImportPhotos.exe --manifest data\photo-import\batch\manifest.json
```

Without a connection variable, this validates local files only. With the variable, it compares the batch with SQL without writing. Add `--commit` after reviewing the result to import atomically. Existing identical photos are retained; differing existing photos require an explicit `--replace-existing`. Clear the connection variable after use.

### Configure and run the API

`EventDeskPhotoApi.exe --configure` reads a JSON object from standard input with `ConnectionString`, `ApiToken`, and `Port` fields. Use port `8765`. Configuration is encrypted with Windows DPAPI under `ProgramData`; supply it from a protected local process. `--console` tests the configured service on loopback.

For service installation, review the host guard in [install_service.ps1](photo_api/install_service.ps1) and replace its `PHOTO-API-HOST` placeholder with your approved machine. Run it from elevated PowerShell with the reviewed binary's absolute path and SHA-256 hash. It installs the service but does not configure secrets or start it. Configure successfully before starting `EventDeskPhotoApi`.

The optional [install_tunnel_service.ps1](photo_api/install_tunnel_service.ps1) has the same approved-host guard and binary-hash requirement. Tunnel configuration takes its token through standard input with `-ConfigureToken` and verifies `-ExpectedTunnelId`. It does not start the service. Create tunnel/VPC resources in your own Cloudflare account and route only the loopback photo API.

Set up the gateway bindings described in [cloudflare/README.md](cloudflare/README.md). Before using your own gateway, review the fixed gateway origins in `app.py`, `cloudflare/worker.py`, and `cloudflare/event_runtime.py` for your deployment. Retain the fixed photo and event routes. Local photo proxying also needs `PARTY_PHOTO_API_TOKEN` in its server environment, set to the gateway's edge access token. Keep backend and edge tokens different. Employee images, manifests, protected configuration, tunnel tokens, and SQL credentials stay outside Git.

## Updates and backups

Back up SQL, the hosted mirror/queue, and signing configuration before migrations. For a local SQLite database, use its online backup API or stop writes before copying the database and any WAL sidecars. Retain the same hosted object namespace and instance when updating code. Replacing a source checkout is not a data migration. Run the checks in [README.md](README.md) before release, then rehearse sign-in, check-in, repeat scans, photo access, outage acceptance, and recovery in an isolated target.
