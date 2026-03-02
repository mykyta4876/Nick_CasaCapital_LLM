"""
Verify statement parsing: extract a statement PDF and print a checklist
so you can compare against the actual PDF.

Usage:
    python src/verify_statement.py samples/Account Statement - December 2025 - Acct Ending 6397.pdf
    python src/verify_statement.py samples/0a81a622_November-2025.pdf
"""

import sys
import json
from pathlib import Path
from dataclasses import asdict

from extractor import extract_and_parse


def main():
    if len(sys.argv) < 2:
        print("Usage: python src/verify_statement.py <statement.pdf>")
        print("\nPrints extracted fields so you can verify against the PDF.")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"VERIFY STATEMENT PARSING: {pdf_path.name}")
    print(f"{'='*60}\n")

    data = extract_and_parse(str(pdf_path))

    # Checklist format: [ ] = compare this on your PDF
    checks = [
        ("Bank name", data.bank_name),
        ("Account number", data.account_number or "(not extracted)"),
        ("Account holder", data.account_holder or "(not extracted)"),
        ("Statement period start", data.statement_period_start or "(not extracted)"),
        ("Statement period end", data.statement_period_end or "(not extracted)"),
        ("Beginning balance", f"${data.beginning_balance:,.2f}" if data.beginning_balance else "$0.00"),
        ("Ending balance", f"${data.ending_balance:,.2f}" if data.ending_balance else "$0.00"),
        ("Total deposits (credits)", f"${data.total_deposits:,.2f}" if data.total_deposits else "$0.00"),
        ("Total withdrawals (debits)", f"${data.total_withdrawals:,.2f}" if data.total_withdrawals else "$0.00"),
        ("Total checks", f"${data.total_checks:,.2f}" if data.total_checks else "$0.00 / N/A"),
        ("Average ledger balance", f"${data.average_ledger_balance:,.2f}" if data.average_ledger_balance else "$0.00 / N/A"),
        ("NSF/overdraft fee count", str(data.nsf_count)),
        ("Negative balance days", str(data.overdraft_days)),
        ("MCA payments detected", str(len(data.mca_payments)) + (" (see list below)" if data.mca_payments else "")),
    ]

    print("Check each line against the first page (and summary section) of your PDF:\n")
    for label, value in checks:
        print(f"  [ ] {label}: {value}")
    if data.mca_payments:
        print("\n  MCA lenders found:")
        for m in data.mca_payments:
            print(f"       - {m.get('lender', '?')}: {m.get('count', 0)} payments, ${m.get('total', 0):,.2f}")

    print(f"\n{'='*60}")
    print("If any value is wrong or '(not extracted)', the parser may need")
    print("adjustment for this bank or statement layout.")
    print(f"{'='*60}\n")

    # Optionally write full JSON to stdout
    if "--json" in sys.argv:
        print(json.dumps(asdict(data), indent=2, default=str))


if __name__ == "__main__":
    main()
