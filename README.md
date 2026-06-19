# Farm Dashboard

Web app for farm supplier analysis (Wynnstay invoices and future suppliers). The desktop tool in `../Wynnstay-Invoices/` is separate and unchanged.

## Quick start (local)

**Easiest:** double-click `run_web.bat`, or:

```powershell
cd Farm-Dashboard-Web
.\run_web.bat
```

**First-time login:** set an admin account before starting (see [`.env.example`](.env.example)):

```powershell
$env:ADMIN_EMAIL="you@example.com"
$env:ADMIN_PASSWORD="yourpassword12chars"
.\run_web.bat
```

Open http://127.0.0.1:8000 — you will be asked to sign in.

## Going live

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for Render hosting, PostgreSQL, `dashboard.cwrtmalle.co.uk` DNS on GoDaddy, and user management.

## What it does

- **Import** — upload a Wynnstay Excel export, pick an invoice date, apply the same transforms as the desktop tool
- **Refresh** — re-run unit conversions, category/farm mapping, and Recent flags on all stored rows
- **View data** — browse invoice lines in the browser

## Database

By default uses SQLite (`data/wynnstay.db`). For production, use PostgreSQL — see `DEPLOYMENT.md` and `.env.example`.

## Optional: migrate from desktop Excel

Read-only import from the desktop `Tool Files` folder:

```bat
python scripts/migrate_from_excel.py --desktop-dir "..\Wynnstay-Invoices"
```

This never writes back to the desktop Excel files.

## Desktop tool

Keep using `../Wynnstay-Invoices/run_invoices.bat` as normal. Both can run in parallel while you evaluate the web version.
