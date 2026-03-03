"""
Underwriting service (Flask) for unread Gmail emails.

Flow per call to /tasks/process-unread:
1. Find unread Gmail messages with PDF attachments.
2. For each message:
   - Download PDF attachments into samples/ (via gmail_fetcher).
   - Classify PDFs into statement PDFs vs application PDFs.
   - Run underwriting pipeline just on those PDFs.
   - Mark the email as read (remove UNREAD label).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple
from dataclasses import asdict
import json

from flask import Flask, jsonify, render_template, abort, request, redirect, url_for

from gmail_fetcher import (
    get_gmail_service,
    list_messages,
    save_attachments_and_metadata,
    mark_as_read,
    EmailMetadata,
)
from extractor import process_statement
from batch_processor import generate_combined_analysis, print_analysis_summary, _classify_pdf
from application_extractor import extract_application_data
from pipeline import (
    build_application_from_batch,
    apply_app_overrides,
    run_underwriting,
)
from underwriting_engine import UnderwritingResult, DeclineReason, Decision

ROOT_DIR = Path(__file__).resolve().parent.parent   # mca-ocr-worker
PROJECT_ROOT = ROOT_DIR.parent                     # Nick_CasaCapital_LLM
SAMPLES_DIR = ROOT_DIR / "samples"
OUTPUT_DIR = ROOT_DIR / "output"
APP_DATA_JSON = ROOT_DIR / "app_data.json"
DEALS_ROOT = PROJECT_ROOT / "casa-capital" / "deals"

app = Flask(__name__, template_folder=str(ROOT_DIR / "templates"))


def classify_pdfs(meta: EmailMetadata) -> Tuple[List[Path], List[Path]]:
    """Split PDFs from one email into statement PDFs vs application PDFs."""
    statement_pdfs: List[Path] = []
    app_pdfs: List[Path] = []

    for rel in meta.pdf_files:
        p = (PROJECT_ROOT / rel).resolve()
        name = p.name.lower()
        # First, use the same content-based classifier as batch_processor
        kind = _classify_pdf(p)
        if kind == "application":
            app_pdfs.append(p)
        elif kind == "statement":
            statement_pdfs.append(p)
        else:
            # Fallback to filename heuristic
            if "application" in name or "_app_" in name:
                app_pdfs.append(p)
            else:
                statement_pdfs.append(p)

    return statement_pdfs, app_pdfs


def process_one_email(meta: EmailMetadata) -> None:
    """
    For a single email:
    - Run statement extraction on its statement PDFs
    - Combine into analysis
    - Apply application / JSON app_data overrides
    - Run underwriting
    """
    statement_pdfs, app_pdfs = classify_pdfs(meta)
    if not statement_pdfs:
        return

    # Use same slug scheme as gmail_fetcher so attachments + JSON sit together
    from gmail_fetcher import deal_slug as gf_deal_slug

    deal_slug = gf_deal_slug(meta.subject, meta.message_id, email_date=getattr(meta, "date", None))
    deal_dir = DEALS_ROOT / deal_slug
    deal_dir.mkdir(parents=True, exist_ok=True)

    # For statement analyses we now write into the deal directory itself
    email_output_dir = deal_dir

    # Extract statements for THIS email only
    results = []
    for pdf_path in statement_pdfs:
        data = process_statement(str(pdf_path), str(email_output_dir))
        results.append({"file": pdf_path.name, "data": data})

    # Combined analysis
    analysis = generate_combined_analysis(results)
    print_analysis_summary(analysis)

    # Save combined analysis JSON into the deal directory
    analysis_path = deal_dir / "analysis.json"
    with analysis_path.open("w", encoding="utf-8") as f:
        json.dump(analysis, f, indent=2)

    # Build ApplicationData from analysis
    app_data = build_application_from_batch(analysis)

    # Application overrides: PDF(s) then global JSON
    for app_pdf in app_pdfs:
        app_data = apply_app_overrides(app_data, app_pdf)

    # Optional per-deal overrides (e.g. fico_score entered manually later)
    override_path = deal_dir / "app_override.json"
    if override_path.exists():
        app_data = apply_app_overrides(app_data, override_path)

    # Snapshot current application data for this deal
    snapshot_path = deal_dir / "app_snapshot.json"
    with snapshot_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(app_data), f, indent=2, default=str)

    # Always run underwriting, even if FICO is missing.
    # Eligibility screening that depends on FICO will be handled later.
    uw_path = deal_dir / "underwriting.json"
    run_underwriting(app_data, label=meta.subject or "email", output_path=uw_path)


def process_unread_emails() -> int:
    """Process unread Gmail messages with PDF attachments. Returns count."""
    service = get_gmail_service()
    user_id = "me"

    query = "is:unread has:attachment filename:pdf"
    messages = list_messages(service, user_id, query, max_results=20)

    if not messages:
        return 0

    processed = 0
    for msg in messages:
        msg_id = msg["id"]
        metas: List[EmailMetadata] = save_attachments_and_metadata(service, user_id, msg_id)
        if not metas:
            mark_as_read(service, user_id, msg_id)
            continue

        for meta in metas:
            process_one_email(meta)
            processed += 1

        mark_as_read(service, user_id, msg_id)

    return processed


@app.post("/tasks/process-unread")
def process_unread_endpoint():
    """HTTP endpoint to trigger processing of unread emails."""
    count = process_unread_emails()
    return jsonify({"status": "ok", "emails_processed": count})


@app.get("/deals")
def list_deals():
    """Simple web UI: list all deals discovered in casa-capital/deals."""
    if not DEALS_ROOT.exists():
        abort(404, description="Deals directory not found.")

    q = (request.args.get("q") or "").strip().lower()
    page = max(int(request.args.get("page", 1) or 1), 1)
    page_size = 20

    deals = []
    for deal_dir in sorted(DEALS_ROOT.iterdir()):
        if not deal_dir.is_dir():
            continue
        slug = deal_dir.name
        meta_path = deal_dir / "email_metadata.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        item = {
            "slug": slug,
            "subject": meta.get("subject", slug),
            "sender": meta.get("sender", ""),
            "date": meta.get("date", ""),
        }
        deals.append(item)

    # Search filter
    if q:
        deals = [
            d
            for d in deals
            if q in d["slug"].lower()
            or q in d["subject"].lower()
            or q in d["sender"].lower()
        ]

    total = len(deals)
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    end = start + page_size
    page_deals = deals[start:end]

    return render_template(
        "deals.html",
        deals=page_deals,
        page=page,
        total_pages=total_pages,
        total=total,
        q=q,
    )


@app.get("/deals/<slug>")
def deal_detail(slug: str):
    """Web UI: show one deal (metadata, analysis, underwriting)."""
    deal_dir = DEALS_ROOT / slug
    if not deal_dir.exists():
        abort(404, description="Deal not found.")

    email_meta = {}
    analysis = {}
    uw = {}
    app_info = {}

    meta_path = deal_dir / "email_metadata.json"
    if meta_path.exists():
        try:
            email_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            email_meta = {}

    analysis_path = deal_dir / "analysis.json"
    if analysis_path.exists():
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        except Exception:
            analysis = {}

    uw_path = deal_dir / "underwriting.json"
    if uw_path.exists():
        try:
            uw = json.loads(uw_path.read_text(encoding="utf-8"))
        except Exception:
            uw = {}

    # Application info: prefer snapshot (includes manual FICO/overrides), else parse PDF
    snapshot_path = deal_dir / "app_snapshot.json"
    if snapshot_path.exists():
        try:
            app_info = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except Exception:
            app_info = {}
    if not app_info:
        pdfs = email_meta.get("pdf_files") or []
        for rel in pdfs:
            p = (PROJECT_ROOT / rel).resolve()
            try:
                kind = _classify_pdf(p)
            except Exception:
                kind = "unknown"
            if kind == "application":
                try:
                    app_info = extract_application_data(str(p))
                except Exception:
                    app_info = {}
                break

    return render_template(
        "deal_detail.html",
        slug=slug,
        email_meta=email_meta,
        analysis=analysis,
        uw=uw,
        app_info=app_info,
    )


@app.post("/deals/<slug>/reunderwrite")
def reunderwrite_deal(slug: str):
    """Update FICO and re-run underwriting for a deal."""
    deal_dir = DEALS_ROOT / slug
    if not deal_dir.exists():
        abort(404, description="Deal not found.")

    # Update or create per-deal override JSON with new FICO
    fico_raw = (request.form.get("fico_score") or "").strip()
    override_path = deal_dir / "app_override.json"
    overrides = {}
    if override_path.exists():
        try:
            overrides = json.loads(override_path.read_text(encoding="utf-8"))
        except Exception:
            overrides = {}

    if fico_raw:
        try:
            fico_val = int(fico_raw)
            if fico_val > 0:
                overrides["fico_score"] = fico_val
        except ValueError:
            pass

    if overrides:
        with override_path.open("w", encoding="utf-8") as f:
            json.dump(overrides, f, indent=2)

    # Rebuild app_data from analysis + app_pdf + overrides
    analysis_path = deal_dir / "analysis.json"
    if not analysis_path.exists():
        abort(400, description="No analysis.json found for this deal.")

    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except Exception:
        abort(400, description="Invalid analysis.json for this deal.")

    app_data = build_application_from_batch(analysis)

    # Apply application PDF data
    meta_path = deal_dir / "email_metadata.json"
    app_pdfs: List[Path] = []
    if meta_path.exists():
        try:
            email_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            email_meta = {}
        pdfs = email_meta.get("pdf_files") or []
        for rel in pdfs:
            p = (PROJECT_ROOT / rel).resolve()
            try:
                kind = _classify_pdf(p)
            except Exception:
                kind = "unknown"
            if kind == "application":
                app_pdfs.append(p)

    for app_pdf in app_pdfs:
        app_data = apply_app_overrides(app_data, app_pdf)

    # Apply per-deal overrides again (including new fico)
    if override_path.exists():
        app_data = apply_app_overrides(app_data, override_path)

    # Snapshot updated app data
    snapshot_path = deal_dir / "app_snapshot.json"
    with snapshot_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(app_data), f, indent=2, default=str)

    # Always run underwriting, even if FICO is missing.
    # Eligibility screening that depends on FICO will be handled later.
    uw_path = deal_dir / "underwriting.json"
    run_underwriting(app_data, label=slug, output_path=uw_path)

    return redirect(url_for("deal_detail", slug=slug))


if __name__ == "__main__":
    import os
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host=host, port=port, debug=debug, use_reloader=False)

