# Quartzweb Certificate Proof

Standalone local proof-of-flow for Lantra Quartzweb certificate retrieval.

This is intentionally separate from CAT. The first goal is to prove the browser flow and identify stable selectors before adding CAT tables, MCP tools, storage, or email dispatch.

## Setup

```powershell
cd C:\Users\Will\Documents\Will\Code\2025\Work\Rural\Rural-Booking\quartzweb-certificate-proof
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
New-Item -ItemType File .env.local
```

Fill `.env.local` with:

- `LANTRA_USERNAME`
- `LANTRA_PASSWORD`
- `QUARTZWEB_ORDER_URL` for one safe test order
- `CERT_ZIP_PASSWORD`
- optional `TARGET_LEARNER_REFS`, comma-separated, if the run should select specific learner registration IDs instead of all learners on the order

Do not commit `.env.local`.

## First Run

Run headed so we can watch Quartzweb:

```powershell
.\.venv\Scripts\Activate.ps1
python .\src\quartzweb_probe.py --headed --pause-after-order
```

The script currently:

- logs into `https://ordering.lantra.co.uk/Home/HomePage.aspx`;
- waits for WebForms idle;
- optionally opens `QUARTZWEB_ORDER_URL`;
- captures screenshots and a Playwright trace;
- dumps visible inputs, buttons, selects, links and accordion-like controls to JSON.

It does **not** click `Get Certificates` yet. That comes after we inspect the order page artifacts and confirm selectors.

## Certificate Download Proof

After confirming the order page is correct, run the explicit download mode:

```powershell
.\.venv\Scripts\python.exe .\src\quartzweb_probe.py --download-certificates --timeout-seconds 600
```

To select specific learners:

```powershell
.\.venv\Scripts\python.exe .\src\quartzweb_probe.py --download-certificates --learner-ref 1255862 --learner-ref 1255863 --learner-ref 1255864 --timeout-seconds 600
```

The script:

- logs in to Quartzweb;
- opens `QUARTZWEB_ORDER_URL`;
- selects the requested learner registration rows, or all learner rows if none are specified;
- expands the `eCertificates` accordion;
- enters `CERT_ZIP_PASSWORD`;
- clicks `Get Certificates`;
- saves the downloaded zip;
- extracts the PDFs with the configured password.

## Output

Each run writes to:

```text
runs/<timestamp>/
```

Useful files:

- `home.png`
- `order.png`
- `controls.json`
- `page.html`
- `trace.zip`
- `run_meta.json`
- `downloads/<zip file>` when `--download-certificates` is used
- `extracted/<certificate folder>/*.pdf` when zip extraction succeeds
- `certificates_meta.json` when `--download-certificates` is used

## Next Proof Step

The browser download flow is proven. The next step is matching extracted certificate PDFs back to CAT booking attendees, then adding a review/send step before any learner emails are dispatched.

## CAT Worker

After CAT has created certificate dispatch jobs from the Gmail trigger, process queued jobs with:

```powershell
.\.venv\Scripts\python.exe .\src\cat_certificate_worker.py --limit 3
```

The worker:

- polls CAT for queued certificate jobs;
- opens the job's Quartzweb order URL;
- downloads and extracts eCertificates;
- uploads each PDF back to CAT;
- asks CAT to send the job.

CAT still owns email delivery. While CAT is in test mode, every email is sent to `CERTIFICATE_EMAIL_TEST_RECIPIENT`, regardless of learner details.

## GitHub Actions

The worker can run on GitHub Actions using `.github/workflows/certificate-worker.yml`.

Add these GitHub repository secrets:

- `LANTRA_USERNAME`
- `LANTRA_PASSWORD`
- `CERT_ZIP_PASSWORD`
- `CAT_CERTIFICATE_WEBHOOK_URL`
- `CERTIFICATE_DISPATCH_WEBHOOK_SECRET`

The workflow runs every 10 minutes and can also be triggered manually from GitHub.
