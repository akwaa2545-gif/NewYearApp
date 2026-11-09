# Deployment

Updated: 2026-11-09

Complete the local setup in [README.md](README.md) first. Keep configuration, passwords, employee data, photos, and generated files outside the published source. Local SQLite and a hosted Durable Object are independent databases; deploying code does not copy attendance between them.

## HTTPS and environment

Use a trusted HTTPS reverse proxy for shared access. Configure these variables in the server process:

| Variable | Purpose |
| --- | --- |
| `PARTY_HOST`, `PARTY_PORT` | Listen address and port; defaults are `0.0.0.0` and `8080` |
| `PARTY_PUBLIC_ORIGIN` | Exact public HTTPS origin, such as `https://event.example.com` |
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

## Optional SQL employee photos

The app works without photos. The optional path is:

```text
Browser -> app photo endpoint -> protected edge gateway
        -> Cloudflare tunnel/VPC binding -> Windows loopback API -> SQL Server
```

The Windows API serves photos only; it does not replace the app's attendance database. The SQL scripts and connection guards currently target a database named `Employee_db` and table `dbo.EventDeskEmployeePhoto`. Photo requests require canonical seven-digit numeric employee IDs; the synthetic alphanumeric sample records do not have matching photos.

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

Set up the gateway bindings described in [cloudflare/README.md](cloudflare/README.md). Before using your own gateway, replace the fixed photo gateway origin in `app.py` and `cloudflare/worker.py` with your deployment's HTTPS origin, retaining the `/v1/employees/{id}/photo` route. Local photo proxying also needs `PARTY_PHOTO_API_TOKEN` in its server environment, set to the gateway's edge access token. Keep backend and edge tokens different. Employee images, manifests, protected configuration, tunnel tokens, and SQL credentials stay outside Git.

## Updates and backups

Back up live SQLite or Durable Object data before migrations. Retain the same hosted object namespace and instance when updating code. Replacing a local source checkout is not a data migration. Run the checks in [README.md](README.md) before release, then verify sign-in, one test check-in, repeat-scan behavior, and photo access in the target environment.
