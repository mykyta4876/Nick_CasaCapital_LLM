"""
Full MCA pipeline: PDF(s) → Analysis → Underwriting

Usage examples:

Single PDF:
    python src/pipeline.py samples/bank_statement.pdf

Folder of PDFs (multiple months):
    python src/pipeline.py samples/
"""

from pathlib import Path
import sys
import json
from dataclasses import asdict
from typing import Dict, Any, Optional, List

from batch_processor import process_batch
from extractor import process_statement
from application_extractor import extract_application_data
from underwriting_engine import (
    ApplicationData,
    UnderwritingEngine,
    UnderwritingResult,
    print_result,
)


def build_application_from_batch(analysis: Dict[str, Any]) -> ApplicationData:
    """
    Build ApplicationData from batch_processor combined analysis.

    This uses averaged metrics from multiple statements as a proxy for
    monthly performance. You can override any fields later in CRM.
    """
    summary = analysis.get("summary", {}) or {}
    monthly = analysis.get("monthly_averages", {}) or {}
    mca = analysis.get("mca_summary", {}) or {}
    risk = analysis.get("risk_indicators", {}) or {}
    mca_positions = analysis.get("mca_positions", {}) or {}

    avg_deposits = float(monthly.get("avg_deposits", 0.0) or 0.0)
    avg_withdrawals = float(monthly.get("avg_withdrawals", 0.0) or 0.0)
    avg_balance = float(monthly.get("avg_ledger_balance", 0.0) or 0.0)
    estimated_mca_burden = float(mca.get("estimated_monthly_burden", 0.0) or 0.0)
    mca_ratio_percent = float(mca.get("mca_to_deposit_ratio", 0.0) or 0.0)

    app = ApplicationData(
        business_name=summary.get("account_holder", "") or "",
        monthly_revenue=avg_deposits,
        true_monthly_revenue=avg_deposits,
        monthly_expenses=avg_withdrawals,
        avg_daily_balance=avg_balance,
        negative_days=int(risk.get("total_negative_balance_days", 0) or 0),
        nsf_count=int(risk.get("total_nsf_overdraft_fees", 0) or 0),
        # Approximate deposit days – can be refined later
        deposit_days_per_month=20,
        existing_positions=len(mca_positions),
        mca_withhold_percent=(mca_ratio_percent / 100.0) if mca_ratio_percent else 0.0,
        total_mca_payments_monthly=estimated_mca_burden,
    )

    return app


def build_application_from_single(statement: Dict[str, Any]) -> ApplicationData:
    """
    Build ApplicationData from a single statement extracted by extractor.process_statement.
    """
    total_deposits = float(statement.get("total_deposits", 0.0) or 0.0)
    total_withdrawals = float(statement.get("total_withdrawals", 0.0) or 0.0)
    total_checks = float(statement.get("total_checks", 0.0) or 0.0)
    total_fees = float(statement.get("total_fees", 0.0) or 0.0)
    avg_balance = float(statement.get("average_ledger_balance", 0.0) or 0.0)
    total_mca = float(statement.get("total_mca_payments", 0.0) or 0.0)

    mca_payments = statement.get("mca_payments") or []

    app = ApplicationData(
        business_name=statement.get("account_holder", "") or "",
        monthly_revenue=total_deposits,
        true_monthly_revenue=total_deposits,
        monthly_expenses=(total_withdrawals + total_checks + total_fees),
        avg_daily_balance=avg_balance,
        negative_days=int(statement.get("overdraft_days", 0) or 0),
        nsf_count=int(statement.get("nsf_count", 0) or 0),
        # Single statement ≈ one month of activity
        deposit_days_per_month=20,
        existing_positions=len(mca_payments),
        mca_withhold_percent=(total_mca / total_deposits) if total_deposits > 0 else 0.0,
        total_mca_payments_monthly=total_mca,
    )

    return app


def apply_app_overrides(
    app: ApplicationData, app_data_path: Optional[Path]
) -> ApplicationData:
    """
    Override ApplicationData fields with values from an application JSON file.

    The JSON can contain any keys that exist on ApplicationData
    (e.g. time_in_business_years, fico_score, industry, state, etc).
    """
    if not app_data_path:
        return app

    if not app_data_path.exists():
        print(f"App data not found at {app_data_path}, ignoring.")
        return app

    # Support both JSON and PDF application files
    overrides: Dict[str, Any]
    try:
        if app_data_path.suffix.lower() == ".pdf":
            print(f"Parsing application PDF: {app_data_path}")
            overrides = extract_application_data(str(app_data_path))
        else:
            with app_data_path.open("r") as f:
                overrides = json.load(f)
    except Exception as exc:
        print(f"Failed to load app data from {app_data_path}: {exc}")
        return app

    if not isinstance(overrides, dict):
        print(f"App data file {app_data_path} must contain a JSON object.")
        return app

    for key, value in overrides.items():
        if hasattr(app, key):
            setattr(app, key, value)

    return app


def run_underwriting(app: ApplicationData, label: str, output_path: Path) -> UnderwritingResult:
    """Run underwriting engine and save JSON result."""
    engine = UnderwritingEngine()
    result = engine.evaluate(app)

    print("\n" + "=" * 70)
    print(f"UNDERWRITING FOR: {label}")
    print("=" * 70)
    print_result(result)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(asdict(result), f, indent=2, default=str)
    print(f"\nSaved underwriting result: {output_path}")

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python src/pipeline.py <pdf_path|folder_of_pdfs> [output_dir] [app_data.json]")
        print("\nExamples:")
        print("  python src/pipeline.py samples/bank_statement.pdf")
        print("  python src/pipeline.py samples/bank_statement.pdf app_data.json")
        print("  python src/pipeline.py samples/ output/ app_data.json")
        sys.exit(1)

    input_arg = sys.argv[1]

    # Default values
    output_dir_arg = "output"
    app_data_paths: List[Path] = []

    # Parse: [input] [output_dir] [app_data_1] [app_data_2]
    # App data can be PDF or JSON; all are applied in order (later overrides earlier).
    if len(sys.argv) >= 3:
        if sys.argv[2].lower().endswith(".json"):
            app_data_paths.append(Path(sys.argv[2]))
        else:
            output_dir_arg = sys.argv[2]
    if len(sys.argv) >= 4:
        output_dir_arg = sys.argv[2]
        app_data_paths.append(Path(sys.argv[3]))
    if len(sys.argv) >= 5:
        app_data_paths.append(Path(sys.argv[4]))

    input_path = Path(input_arg)
    output_dir = Path(output_dir_arg)

    if not input_path.exists():
        print(f"Input not found: {input_path}")
        sys.exit(1)

    if input_path.is_dir():
        # Multiple PDFs → combined analysis → underwriting
        analysis = process_batch(str(input_path), str(output_dir))
        if not analysis:
            print("No analysis generated from batch.")
            sys.exit(1)

        app = build_application_from_batch(analysis)
        for path in app_data_paths:
            app = apply_app_overrides(app, path)
        uw_path = output_dir / "combined_underwriting.json"
        run_underwriting(app, label=str(input_path), output_path=uw_path)

    else:
        # Single PDF → single statement analysis → underwriting
        if input_path.suffix.lower() != ".pdf":
            print(f"Expected a .pdf file, got: {input_path}")
            sys.exit(1)

        statement = process_statement(str(input_path), str(output_dir))
        app = build_application_from_single(statement)
        for path in app_data_paths:
            app = apply_app_overrides(app, path)
        uw_path = output_dir / f"{input_path.stem}_underwriting.json"
        run_underwriting(app, label=input_path.name, output_path=uw_path)


if __name__ == "__main__":
    main()

