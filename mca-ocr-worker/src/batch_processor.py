"""
Batch processor for multiple bank statements
Generates combined underwriting analysis
"""

from pathlib import Path
import json
from datetime import datetime
from dataclasses import asdict

from extractor import process_statement, BankStatementData

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


def _classify_pdf(pdf_path: Path) -> str:
    """
    Heuristically classify a PDF as 'statement', 'application', or 'unknown'
    based on text content from the first page.
    """
    # Quick filename-based hint first
    name_lower = pdf_path.name.lower()
    if "application" in name_lower:
        return "application"

    if pdfplumber is None:
        return "unknown"

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return "unknown"
            text = (pdf.pages[0].extract_text() or "").lower()
    except Exception:
        return "unknown"

    # Keywords that strongly indicate a bank statement
    statement_keywords = [
        "account statement",
        "business checking account statement",
        "statement ending",
        "statement summary",
        "summary of accounts",
        "account activity",
        "daily balances",
        "checks cleared",
        "fees summary",
    ]
    # Keywords that strongly indicate a funding/application form
    application_keywords = [
        "funding application",
        "merchant application",
        "business funding application",
        "legal name",
        "business information",
        "business start date",
        "time in business",
        "est. fico",
        "use of proceeds",
        "use of funds",
        "requested amount",
        "primary owner",
        "owner 1 signature",
        "owner 2 signature",
    ]

    stmt_hits = sum(1 for k in statement_keywords if k in text)
    app_hits = sum(1 for k in application_keywords if k in text)

    # If we clearly see more statement signals than application signals
    if stmt_hits >= 1 and app_hits == 0:
        return "statement"
    # If we clearly see multiple application signals and no statement ones
    if app_hits >= 2 and stmt_hits == 0:
        return "application"

    return "unknown"


def process_batch(input_dir: str, output_dir: str = "output") -> dict:
    """
    Process all PDFs in a directory and generate combined analysis
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # Find all PDFs (dedupe for case-insensitive filesystems)
    raw_pdfs = list(input_path.glob("*.pdf")) + list(input_path.glob("*.PDF"))
    pdfs_by_key = {}
    for p in raw_pdfs:
        key = str(p.resolve()).lower()
        if key not in pdfs_by_key:
            pdfs_by_key[key] = p
    pdfs = list(pdfs_by_key.values())

    # Filter out application PDFs so only bank statements are analyzed in batch mode
    statement_pdfs = []
    for pdf in pdfs:
        kind = _classify_pdf(pdf)
        if kind == "application":
            print(f"Skipping application PDF (not a bank statement): {pdf.name}")
            continue
        statement_pdfs.append(pdf)

    pdfs = statement_pdfs
    
    if not pdfs:
        print(f"No statement PDFs found in {input_dir}")
        return {}
    
    print(f"\n{'='*60}")
    print(f"BATCH PROCESSING: {len(pdfs)} statements")
    print(f"{'='*60}")
    
    results = []
    errors = []
    
    for pdf in sorted(pdfs):
        try:
            result = process_statement(str(pdf), str(output_path))
            results.append({
                'file': pdf.name,
                'data': result
            })
        except Exception as e:
            print(f"ERROR processing {pdf.name}: {e}")
            errors.append({
                'file': pdf.name,
                'error': str(e)
            })
    
    # Generate combined analysis
    analysis = generate_combined_analysis(results)
    analysis['errors'] = errors
    analysis['processed_files'] = [r['file'] for r in results]
    
    # Save combined report
    report_path = output_path / "combined_analysis.json"
    with open(report_path, 'w') as f:
        json.dump(analysis, f, indent=2)
    
    # Print summary
    print_analysis_summary(analysis)
    
    print(f"\nCombined report saved: {report_path}")
    
    return analysis


def generate_combined_analysis(results: list) -> dict:
    """Generate combined underwriting metrics from multiple statements"""
    
    if not results:
        return {}
    
    statements = [r['data'] for r in results]
    
    # Calculate averages
    avg_deposits = sum(s['total_deposits'] for s in statements) / len(statements)
    avg_withdrawals = sum(s['total_withdrawals'] for s in statements) / len(statements)
    avg_balance = sum(s['average_ledger_balance'] for s in statements) / len(statements)
    
    # Find all MCA lenders
    mca_lenders = {}
    for s in statements:
        for mca in s.get('mca_payments', []):
            lender = mca['lender']
            if lender not in mca_lenders:
                mca_lenders[lender] = {
                    'total_payments': 0,
                    'payment_count': 0,
                    'months_present': 0
                }
            mca_lenders[lender]['total_payments'] += mca['total']
            mca_lenders[lender]['payment_count'] += mca['count']
            mca_lenders[lender]['months_present'] += 1
    
    # Calculate estimated monthly MCA burden
    total_mca_monthly = sum(s['total_mca_payments'] for s in statements) / len(statements)
    
    # Risk indicators
    total_nsf = sum(s.get('nsf_count', 0) for s in statements)
    total_overdraft_days = sum(s.get('overdraft_days', 0) for s in statements)
    max_nsf_per_period = max((s.get('nsf_count', 0) for s in statements), default=0)
    max_overdraft_days_per_period = max((s.get('overdraft_days', 0) for s in statements), default=0)
    
    # Ending balance trend
    ending_balances = [s['ending_balance'] for s in statements]
    balance_trend = "stable"
    if len(ending_balances) >= 2:
        if ending_balances[-1] > ending_balances[0] * 1.1:
            balance_trend = "improving"
        elif ending_balances[-1] < ending_balances[0] * 0.9:
            balance_trend = "declining"
    
    # Calculate available monthly revenue (deposits minus MCA payments)
    available_revenue = avg_deposits - total_mca_monthly
    
    # Estimate max additional MCA payment capacity
    # Rule: Total MCA payments should not exceed 15-20% of deposits
    max_mca_capacity = avg_deposits * 0.15
    additional_capacity = max(0, max_mca_capacity - total_mca_monthly)

    # Cash buffer vs daily MCA (MoneyThumb-style flag)
    daily_mca = (total_mca_monthly / 22.0) if total_mca_monthly > 0 else 0.0
    buffer_multiple = (avg_balance / daily_mca) if daily_mca > 0 else 0.0
    
    # Derive overall period covered (min start date to max end date)
    from datetime import datetime

    def _parse_date(d: str):
        for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                return datetime.strptime(d, fmt)
            except Exception:
                continue
        return None

    # Order statements by start date for month-by-month view and revenue trend
    enriched = []
    for s in statements:
        start_raw = s.get("statement_period_start")
        start_dt = _parse_date(start_raw) if start_raw else None
        enriched.append((start_dt, s))
    enriched.sort(key=lambda x: (x[0] or datetime.min))
    ordered_statements = [s for _, s in enriched]

    starts = [s.get("statement_period_start") for s in ordered_statements if s.get("statement_period_start")]
    ends = [s.get("statement_period_end") for s in ordered_statements if s.get("statement_period_end")]

    parsed_starts = [(_parse_date(d), d) for d in starts if _parse_date(d)]
    parsed_ends = [(_parse_date(d), d) for d in ends if _parse_date(d)]

    if parsed_starts:
        overall_start = min(parsed_starts, key=lambda x: x[0])[1]
    else:
        overall_start = ordered_statements[0].get("statement_period_start", "N/A")

    if parsed_ends:
        overall_end = max(parsed_ends, key=lambda x: x[0])[1]
    else:
        overall_end = ordered_statements[-1].get("statement_period_end", "N/A")

    # Month-by-month rows (approximate)
    month_rows = []
    for idx, s in enumerate(ordered_statements):
        start_label = s.get("statement_period_start", "") or ""
        end_label = s.get("statement_period_end", "") or ""
        label = f"Period {idx + 1}"
        # Prefer the ending date's month/year (e.g. Nov 29–Dec 31 -> Dec 2025)
        dt_end = _parse_date(end_label) if end_label else None
        dt_start = _parse_date(start_label) if start_label else None
        dt = dt_end or dt_start
        if dt:
            label = dt.strftime("%b %Y")
        deposits = s.get("total_deposits", 0)
        end_bal = s.get("ending_balance", 0)
        nsf = s.get("nsf_count", 0)
        qual = "good" if nsf == 0 else "warn" if nsf <= 5 else "bad"
        month_rows.append(
            {
                "label": label,
                "deposits": round(deposits, 2),
                "ending_balance": round(end_bal, 2),
                "nsf": int(nsf),
                "qual": qual,
            }
        )

    # Revenue trend / decline %
    revenue_decline_pct = 0.0
    revenue_trend = "stable"
    if len(ordered_statements) >= 2:
        first_dep = ordered_statements[0].get("total_deposits", 0) or 0
        last_dep = ordered_statements[-1].get("total_deposits", 0) or 0
        if first_dep > 0:
            change = (last_dep - first_dep) / first_dep * 100.0
            if change < 0:
                revenue_decline_pct = round(abs(change), 1)
                if abs(change) > 10:
                    revenue_trend = "declining"
            elif change > 10:
                revenue_trend = "improving"

    return {
        'summary': {
            'statements_analyzed': len(statements),
            'period_covered': f"{overall_start} to {overall_end}",
            'account_holder': ordered_statements[0].get('account_holder', 'N/A'),
            'bank': ordered_statements[0].get('bank_name', 'N/A'),
        },
        'monthly_averages': {
            'avg_deposits': round(avg_deposits, 2),
            'avg_withdrawals': round(avg_withdrawals, 2),
            'avg_ledger_balance': round(avg_balance, 2),
            'avg_mca_payments': round(total_mca_monthly, 2),
            'available_after_mca': round(available_revenue, 2),
        },
        'month_breakdown': month_rows,
        'revenue_decline_percent': revenue_decline_pct,
        'revenue_trend': revenue_trend,
        'mca_positions': mca_lenders,
        'mca_summary': {
            'total_lenders': len(mca_lenders),
            'estimated_monthly_burden': round(total_mca_monthly, 2),
            'mca_to_deposit_ratio': round((total_mca_monthly / avg_deposits * 100) if avg_deposits > 0 else 0, 1),
            'additional_capacity_15pct': round(additional_capacity, 2),
        },
        'risk_indicators': {
            'total_nsf_overdraft_fees': total_nsf,
            'total_negative_balance_days': total_overdraft_days,
            'max_nsf_per_period': int(max_nsf_per_period),
            'max_overdraft_days_per_period': int(max_overdraft_days_per_period),
            'balance_trend': balance_trend,
            'ending_balances': ending_balances,
            'cash_buffer_multiple': round(buffer_multiple, 2),
        },
        'underwriting_recommendation': generate_recommendation(
            avg_deposits, total_mca_monthly, total_nsf, total_overdraft_days, avg_balance
        )
    }


def generate_recommendation(avg_deposits: float, mca_burden: float, 
                          nsf_count: int, overdraft_days: int, 
                          avg_balance: float) -> dict:
    """Generate underwriting recommendation based on metrics"""
    
    score = 100  # Start with perfect score
    flags = []
    
    # MCA burden ratio
    mca_ratio = (mca_burden / avg_deposits * 100) if avg_deposits > 0 else 0
    if mca_ratio > 20:
        score -= 30
        flags.append(f"HIGH MCA burden: {mca_ratio:.1f}% of deposits")
    elif mca_ratio > 15:
        score -= 15
        flags.append(f"MODERATE MCA burden: {mca_ratio:.1f}% of deposits")
    elif mca_ratio > 10:
        score -= 5
        flags.append(f"Existing MCA positions: {mca_ratio:.1f}% of deposits")
    
    # NSF/Overdrafts
    if nsf_count > 10:
        score -= 25
        flags.append(f"EXCESSIVE NSF activity: {nsf_count} instances")
    elif nsf_count > 5:
        score -= 15
        flags.append(f"HIGH NSF activity: {nsf_count} instances")
    elif nsf_count > 0:
        score -= 5
        flags.append(f"Some NSF activity: {nsf_count} instances")
    
    # Negative balance days
    if overdraft_days > 15:
        score -= 25
        flags.append(f"FREQUENT negative balances: {overdraft_days} days")
    elif overdraft_days > 5:
        score -= 15
        flags.append(f"Occasional negative balances: {overdraft_days} days")
    elif overdraft_days > 0:
        score -= 5
        flags.append(f"Rare negative balances: {overdraft_days} days")
    
    # Average balance health
    if avg_balance < 0:
        score -= 20
        flags.append(f"NEGATIVE average balance: ${avg_balance:,.2f}")
    elif avg_balance < 1000:
        score -= 10
        flags.append(f"LOW average balance: ${avg_balance:,.2f}")
    
    # Monthly deposits
    if avg_deposits < 20000:
        score -= 10
        flags.append(f"LOW monthly deposits: ${avg_deposits:,.2f}")
    
    # Determine decision
    score = max(0, score)
    
    if score >= 80:
        decision = "AUTO_APPROVE"
        tier = "A"
    elif score >= 60:
        decision = "MANUAL_REVIEW"
        tier = "B"
    elif score >= 40:
        decision = "MANUAL_REVIEW"
        tier = "C"
    else:
        decision = "AUTO_DECLINE"
        tier = "D"
    
    # Calculate max offer based on deposits and existing burden
    available = avg_deposits - mca_burden
    max_advance = available * 0.5 * 1.3  # 50% of available * 1.3 factor
    max_advance = max(0, min(max_advance, 150000))  # Cap at 150k
    
    return {
        'score': score,
        'tier': tier,
        'decision': decision,
        'flags': flags,
        'max_recommended_advance': round(max_advance, 2),
        'recommended_daily_payment': round(max_advance * 1.35 / 120, 2) if max_advance > 0 else 0,  # 1.35 factor, 120 days
    }


def print_analysis_summary(analysis: dict):
    """Print formatted analysis summary"""
    
    print("\n" + "=" * 60)
    print("COMBINED UNDERWRITING ANALYSIS")
    print("=" * 60)
    
    summary = analysis.get('summary', {})
    print(f"\nAccount: {summary.get('account_holder', 'N/A')}")
    print(f"Bank: {summary.get('bank', 'N/A')}")
    print(f"Period: {summary.get('period_covered', 'N/A')}")
    print(f"Statements Analyzed: {summary.get('statements_analyzed', 0)}")
    
    monthly = analysis.get('monthly_averages', {})
    print(f"\n📊 MONTHLY AVERAGES:")
    print(f"   Deposits:       ${monthly.get('avg_deposits', 0):>12,.2f}")
    print(f"   Withdrawals:    ${monthly.get('avg_withdrawals', 0):>12,.2f}")
    print(f"   Avg Balance:    ${monthly.get('avg_ledger_balance', 0):>12,.2f}")
    print(f"   MCA Payments:   ${monthly.get('avg_mca_payments', 0):>12,.2f}")
    print(f"   Net Available:  ${monthly.get('available_after_mca', 0):>12,.2f}")
    
    mca = analysis.get('mca_summary', {})
    if mca.get('total_lenders', 0) > 0:
        print(f"\n⚠️  MCA POSITIONS: {mca.get('total_lenders', 0)} lenders")
        print(f"   Monthly Burden: ${mca.get('estimated_monthly_burden', 0):,.2f}")
        print(f"   Burden Ratio:   {mca.get('mca_to_deposit_ratio', 0):.1f}% of deposits")
        
        for lender, data in analysis.get('mca_positions', {}).items():
            print(f"   - {lender}: ${data['total_payments']:,.2f} ({data['payment_count']} payments)")
    
    risk = analysis.get('risk_indicators', {})
    print(f"\n🚨 RISK INDICATORS:")
    print(f"   NSF/Overdraft Fees: {risk.get('total_nsf_overdraft_fees', 0)}")
    print(f"   Negative Balance Days: {risk.get('total_negative_balance_days', 0)}")
    print(f"   Balance Trend: {risk.get('balance_trend', 'N/A')}")
    
    rec = analysis.get('underwriting_recommendation', {})
    print(f"\n✅ RECOMMENDATION:")
    print(f"   Score: {rec.get('score', 0)}/100")
    print(f"   Tier: {rec.get('tier', 'N/A')}")
    print(f"   Decision: {rec.get('decision', 'N/A')}")
    print(f"   Max Advance: ${rec.get('max_recommended_advance', 0):,.2f}")
    print(f"   Daily Payment: ${rec.get('recommended_daily_payment', 0):,.2f}")
    
    if rec.get('flags'):
        print(f"\n   Flags:")
        for flag in rec['flags']:
            print(f"   ⚠️  {flag}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python batch_processor.py <input_dir> [output_dir]")
        print("\nExample:")
        print("  python batch_processor.py samples/ output/")
        sys.exit(1)
    
    input_dir = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    
    process_batch(input_dir, output_dir)
