from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


CERTIFICATE_FILE_RE = re.compile(r"^eCertificate\s+(.+?),\s+(.+?)\s+\[(\d+)\].*\.pdf$", re.IGNORECASE)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_environment() -> None:
    load_dotenv(repo_root() / ".env.local")
    load_dotenv(repo_root() / ".env", override=False)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def cat_base_url() -> str:
    explicit = os.getenv("CAT_CERTIFICATE_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    webhook = require_env("CAT_CERTIFICATE_WEBHOOK_URL")
    marker = "/api/certificates/lantra-email-trigger"
    if marker not in webhook:
        raise RuntimeError("CAT_CERTIFICATE_WEBHOOK_URL does not look like the certificate trigger URL.")
    return webhook.split(marker, 1)[0].rstrip("/")


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {require_env('CERTIFICATE_DISPATCH_WEBHOOK_SECRET')}"}


def list_queued_jobs(limit: int) -> list[dict[str, Any]]:
    response = requests.get(
        f"{cat_base_url()}/api/certificates/dispatch-jobs/queued",
        params={"limit": limit},
        headers=auth_headers(),
        timeout=60,
    )
    response.raise_for_status()
    return response.json().get("jobs", [])


def run_quartz_download(order_url: str) -> Path:
    command = [
        sys.executable,
        str(repo_root() / "src" / "quartzweb_probe.py"),
        "--order-url",
        order_url,
        "--download-certificates",
        "--timeout-seconds",
        "600",
    ]
    result = subprocess.run(command, cwd=repo_root(), text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"Quartzweb download failed:\n{result.stdout}\n{result.stderr}")

    artifact_line = next((line for line in result.stdout.splitlines() if line.startswith("Artifacts: ")), None)
    if not artifact_line:
        raise RuntimeError(f"Quartzweb download did not report an artifact directory:\n{result.stdout}")
    return Path(artifact_line.removeprefix("Artifacts: ").strip())


def read_certificate_files(run_dir: Path) -> list[dict[str, str]]:
    meta_path = run_dir / "certificates_meta.json"
    if not meta_path.exists():
        raise RuntimeError(f"Missing certificate metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    extract_dir = Path(meta["extractDir"])
    files: list[dict[str, str]] = []
    for relative in meta.get("extraction", {}).get("files", []):
        path = extract_dir / relative
        name = path.name
        match = CERTIFICATE_FILE_RE.match(name)
        if not match:
            raise RuntimeError(f"Unable to parse learner details from certificate filename: {name}")
        last_name, first_name, learner_ref = match.groups()
        files.append({
            "path": str(path),
            "filename": name,
            "learnerName": f"{first_name.strip()} {last_name.strip()}",
            "learnerRegistrationId": learner_ref.strip(),
        })
    return files


def upload_certificate(job_id: str, certificate: dict[str, str]) -> dict[str, Any]:
    path = Path(certificate["path"])
    with path.open("rb") as handle:
      response = requests.post(
          f"{cat_base_url()}/api/certificates/dispatch-jobs/{job_id}/items",
          headers=auth_headers(),
          files={"certificate": (certificate["filename"], handle, "application/pdf")},
          data={
              "learnerName": certificate["learnerName"],
              "learnerRegistrationId": certificate["learnerRegistrationId"],
              "matchConfidence": "1",
              "status": "matched",
          },
          timeout=120,
      )
    response.raise_for_status()
    return response.json()


def send_job(job_id: str) -> dict[str, Any]:
    response = requests.post(
        f"{cat_base_url()}/api/certificates/dispatch-jobs/{job_id}/send",
        headers=auth_headers(),
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def process_job(job: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    job_id = job["id"]
    order_url = job.get("orderUrl")
    if not order_url:
        raise RuntimeError(f"Job {job_id} has no orderUrl.")

    print(f"Processing job {job_id} order {job.get('lantraOrderNumber') or 'unknown'}")
    if dry_run:
        return {"jobId": job_id, "dryRun": True, "orderUrl": order_url}

    run_dir = run_quartz_download(order_url)
    certificates = read_certificate_files(run_dir)
    uploads = []
    for certificate in certificates:
        upload = upload_certificate(job_id, certificate)
        uploads.append(upload)
        print(
            f"Uploaded {certificate['learnerRegistrationId']} {certificate['learnerName']} "
            f"status={upload.get('status')} jobStatus={upload.get('jobStatus')}"
        )

    send = send_job(job_id)
    print(f"Sent job {job_id}: sent={send.get('sent')} failed={send.get('failed')} status={send.get('jobStatus')}")
    return {
        "jobId": job_id,
        "runDir": str(run_dir),
        "certificateCount": len(certificates),
        "uploads": uploads,
        "send": send,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process queued CAT certificate dispatch jobs.")
    parser.add_argument("--limit", type=int, default=3, help="Maximum queued jobs to process.")
    parser.add_argument("--dry-run", action="store_true", help="List/process no external Quartzweb or send actions.")
    return parser.parse_args()


def main() -> int:
    load_environment()
    args = parse_args()
    jobs = list_queued_jobs(args.limit)
    if not jobs:
        print("No queued certificate dispatch jobs.")
        return 0

    results = []
    for job in jobs:
        try:
            results.append(process_job(job, args.dry_run))
        except Exception as error:
            print(f"Job {job.get('id')} failed: {error}", file=sys.stderr)
            return 1

    print(json.dumps({"processed": len(results), "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
