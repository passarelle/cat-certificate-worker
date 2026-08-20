from __future__ import annotations

import argparse
import zipfile
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyzipper
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


HOME_URL = "https://ordering.lantra.co.uk/Home/HomePage.aspx"
WEBFORMS_IDLE_JS = """
() => {
  try {
    const prm = window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager
      ? Sys.WebForms.PageRequestManager.getInstance()
      : null;
    return !prm || !prm.get_isInAsyncPostBack();
  } catch (e) {
    return true;
  }
}
"""


def now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def redact(value: str | None) -> str | None:
    if not value:
        return None
    return f"<redacted:{len(value)}>"


def wait_for_webforms_idle(page: Page, timeout_ms: int) -> None:
    try:
        page.wait_for_function(WEBFORMS_IDLE_JS, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        # Quartzweb sometimes leaves WebForms state unavailable; callers still capture artifacts.
        pass


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned[:120] or "artifact"


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_learner_refs(raw_values: list[str] | None, env_value: str | None) -> list[str]:
    values: list[str] = []
    for value in raw_values or []:
        values.extend(value.split(","))
    if env_value:
        values.extend(env_value.split(","))
    return [value.strip() for value in values if value.strip()]


def capture_controls(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(
        """
        () => {
          const nodes = Array.from(document.querySelectorAll(
            'input, button, select, textarea, a, summary, [role], [data-toggle], .accordion, .card-header'
          ));
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width >= 0 && rect.height >= 0;
          };
          return nodes.filter(visible).map((el) => ({
            tag: el.tagName.toLowerCase(),
            id: el.id || null,
            name: el.getAttribute('name'),
            type: el.getAttribute('type'),
            role: el.getAttribute('role'),
            title: el.getAttribute('title'),
            ariaLabel: el.getAttribute('aria-label'),
            href: el.getAttribute('href'),
            value: el.tagName.toLowerCase() === 'input' && ['password', 'hidden'].includes((el.getAttribute('type') || '').toLowerCase())
              ? '<redacted>'
              : el.getAttribute('value'),
            text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),
            classes: el.className || null,
          }));
        }
        """
    )


def save_artifacts(page: Page, output_dir: Path, label: str) -> None:
    page.screenshot(path=str(output_dir / f"{safe_filename(label)}.png"), full_page=True)
    if label == "order":
        (output_dir / "page.html").write_text(page.content(), encoding="utf-8")
        write_json(output_dir / "controls.json", capture_controls(page))


def get_available_learner_refs(page: Page) -> list[str]:
    return page.evaluate(
        """
        () => Array.from(document.querySelectorAll('input[id*="hfSelectLearner"]'))
          .map((input) => input.value)
          .filter(Boolean)
        """
    )


def select_learners(page: Page, requested_refs: list[str], timeout_ms: int) -> list[str]:
    available_refs = get_available_learner_refs(page)
    target_refs = requested_refs or available_refs
    if not target_refs:
        raise RuntimeError("No learner registration IDs were found on the order page.")

    missing_refs = [ref for ref in target_refs if ref not in available_refs]
    if missing_refs:
        raise RuntimeError(f"Requested learner registration IDs were not found: {', '.join(missing_refs)}")

    for learner_ref in target_refs:
        hidden_ref = page.locator(
            f'input[id^="ContentPlaceHolder1_gvLearners_hfSelectLearner_"][value="{learner_ref}"]'
        )
        hidden_ref.wait_for(state="attached", timeout=timeout_ms)
        row = hidden_ref.locator("xpath=ancestor::tr")
        checkbox = row.locator('input[type="checkbox"][id*="chkSelectLearner"]')
        checkbox.check(timeout=timeout_ms)

    return target_refs


def expand_ecertificates(page: Page, timeout_ms: int) -> None:
    panel = page.locator("#CertificatesCollapse")
    if panel.count() == 0:
        raise RuntimeError("Could not find the eCertificates accordion panel.")

    if not panel.first.is_visible():
        page.click('a[href="#CertificatesCollapse"]', timeout=timeout_ms)
        panel.first.wait_for(state="visible", timeout=timeout_ms)


def extract_zip(zip_path: Path, password: str, extract_dir: Path) -> dict[str, Any]:
    extract_dir.mkdir(parents=True, exist_ok=True)
    password_bytes = password.encode("utf-8")

    try:
        with pyzipper.AESZipFile(zip_path) as archive:
            names = archive.namelist()
            archive.extractall(extract_dir, pwd=password_bytes)
            return {"method": "pyzipper", "files": names}
    except Exception as pyzipper_error:
        try:
            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
                archive.extractall(extract_dir, pwd=password_bytes)
                return {"method": "zipfile", "files": names}
        except Exception as zipfile_error:
            return {
                "method": None,
                "files": [],
                "error": f"pyzipper: {type(pyzipper_error).__name__}: {pyzipper_error}; "
                f"zipfile: {type(zipfile_error).__name__}: {zipfile_error}",
            }


def download_certificates(
    page: Page,
    output_dir: Path,
    password: str,
    learner_refs: list[str],
    timeout_ms: int,
) -> dict[str, Any]:
    selected_refs = select_learners(page, learner_refs, timeout_ms)
    expand_ecertificates(page, timeout_ms)

    password_input = page.locator("#ContentPlaceHolder1_txtZipPassword")
    password_input.wait_for(state="visible", timeout=timeout_ms)
    password_input.fill(password)

    downloads_dir = output_dir / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    print("Requesting certificate zip from Quartzweb...")
    with page.expect_download(timeout=max(timeout_ms, 60_000)) as download_info:
        page.click("#ContentPlaceHolder1_btnGetCerts", timeout=timeout_ms)

    download = download_info.value
    suggested_name = safe_filename(download.suggested_filename or f"certificates-{now_slug()}.zip")
    zip_path = downloads_dir / suggested_name
    download.save_as(str(zip_path))

    extract_dir = output_dir / "extracted"
    extract_result = extract_zip(zip_path, password, extract_dir)
    result = {
        "selectedLearnerRefs": selected_refs,
        "zipPath": str(zip_path),
        "zipSizeBytes": zip_path.stat().st_size,
        "extractDir": str(extract_dir),
        "extraction": extract_result,
    }
    write_json(output_dir / "certificates_meta.json", result)
    return result


def login(page: Page, username: str, password: str, timeout_ms: int) -> None:
    print("Opening Quartzweb login page...")
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    print("Submitting login form...")
    page.fill("#txtUsername", username)
    page.fill("#txtPassword", password)
    page.click("#btnLogin")
    print("Waiting for authenticated home page...")
    page.wait_for_selector("text=Claim certificates", timeout=timeout_ms)
    if page.locator("#txtUsername").is_visible():
        raise RuntimeError("Login did not complete; Quartzweb still shows the sign-in form.")
    wait_for_webforms_idle(page, timeout_ms)
    print("Login complete.")


def run(args: argparse.Namespace) -> int:
    load_dotenv(repo_root() / ".env.local")
    load_dotenv(repo_root() / ".env", override=False)
    username = os.getenv("LANTRA_USERNAME")
    password = os.getenv("LANTRA_PASSWORD")
    order_url = args.order_url or os.getenv("QUARTZWEB_ORDER_URL")
    certificate_password = os.getenv("CERT_ZIP_PASSWORD")
    learner_refs = parse_learner_refs(args.learner_ref, os.getenv("TARGET_LEARNER_REFS"))

    if not username or not password:
        print("Missing LANTRA_USERNAME or LANTRA_PASSWORD in .env/environment.", file=sys.stderr)
        return 2
    if args.download_certificates and not certificate_password:
        print("Missing CERT_ZIP_PASSWORD in .env/environment.", file=sys.stderr)
        return 2
    if args.download_certificates and not order_url:
        print("Downloading certificates requires --order-url or QUARTZWEB_ORDER_URL.", file=sys.stderr)
        return 2

    timeout_ms = args.timeout_seconds * 1000
    output_dir = repo_root() / "runs" / now_slug()
    output_dir.mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "headed": args.headed,
        "homeUrl": HOME_URL,
        "orderUrl": order_url,
        "username": redact(username),
        "timeoutSeconds": args.timeout_seconds,
        "downloadCertificates": args.download_certificates,
        "requestedLearnerRefs": learner_refs,
        "status": "started",
    }
    write_json(output_dir / "run_meta.json", meta)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo_ms)
        context = browser.new_context(accept_downloads=True)
        context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        try:
            login(page, username, password, timeout_ms)
            save_artifacts(page, output_dir, "home")

            if order_url:
                print(f"Opening order URL ending: ...{order_url[-80:]}")
                page.goto(order_url, wait_until="domcontentloaded", timeout=timeout_ms)
                wait_for_webforms_idle(page, timeout_ms)
                save_artifacts(page, output_dir, "order")
                print("Order page artifacts captured.")

                if args.download_certificates:
                    download_result = download_certificates(
                        page,
                        output_dir,
                        certificate_password or "",
                        learner_refs,
                        timeout_ms,
                    )
                    save_artifacts(page, output_dir, "post-certificates")
                    meta["certificateDownload"] = {
                        "selectedLearnerRefs": download_result["selectedLearnerRefs"],
                        "zipPath": download_result["zipPath"],
                        "zipSizeBytes": download_result["zipSizeBytes"],
                        "extractDir": download_result["extractDir"],
                        "extractedFiles": download_result["extraction"].get("files", []),
                        "extractionMethod": download_result["extraction"].get("method"),
                        "extractionError": download_result["extraction"].get("error"),
                    }
                    print(f"Certificate zip saved: {download_result['zipPath']}")
                    if download_result["extraction"].get("error"):
                        print(f"Zip saved, but extraction failed: {download_result['extraction']['error']}", file=sys.stderr)
                    else:
                        print(f"Extracted files: {len(download_result['extraction'].get('files', []))}")

            if args.pause_after_order:
                print("Paused for manual inspection. Press Enter here to close the browser.")
                input()

            meta["status"] = "ok"
            return 0
        except Exception as error:
            meta["status"] = "failed"
            meta["error"] = f"{type(error).__name__}: {error}"
            try:
                page.screenshot(path=str(output_dir / "failure.png"), full_page=True)
                (output_dir / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            print(meta["error"], file=sys.stderr)
            return 1
        finally:
            meta["finishedAt"] = datetime.now(timezone.utc).isoformat()
            write_json(output_dir / "run_meta.json", meta)
            try:
                context.tracing.stop(path=str(output_dir / "trace.zip"))
            finally:
                context.close()
                browser.close()
            print(f"Artifacts: {output_dir}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quartzweb certificate proof-of-flow probe.")
    parser.add_argument("--headed", action="store_true", help="Run with a visible Chromium window.")
    parser.add_argument("--order-url", help="Quartzweb order URL. Defaults to QUARTZWEB_ORDER_URL.")
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--slow-mo-ms", type=int, default=0)
    parser.add_argument("--pause-after-order", action="store_true", help="Pause before closing so selectors can be inspected manually.")
    parser.add_argument(
        "--download-certificates",
        action="store_true",
        help="Select learners, enter CERT_ZIP_PASSWORD, click Get Certificates and save the encrypted zip.",
    )
    parser.add_argument(
        "--learner-ref",
        action="append",
        help="Learner registration ID to select. Repeat or comma-separate. Defaults to all learners on the order.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
