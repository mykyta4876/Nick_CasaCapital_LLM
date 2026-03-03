"""
MCA Bank Statement Extractor
- Detects native vs scanned PDFs
- Uses pdfplumber for native (free, instant, 100% accurate)
- Falls back to Surya OCR for scanned (GPU accelerated)
"""

import pdfplumber
import fitz  # PyMuPDF
from pathlib import Path
import json
import re
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, asdict


@dataclass
class BankStatementData:
    """Structured output for bank statement analysis"""
    # Metadata
    bank_name: str = ""
    account_number: str = ""
    account_holder: str = ""
    statement_period_start: str = ""
    statement_period_end: str = ""
    
    # Summary
    beginning_balance: float = 0.0
    ending_balance: float = 0.0
    total_deposits: float = 0.0
    total_withdrawals: float = 0.0
    total_checks: float = 0.0
    total_fees: float = 0.0
    
    # Counts
    num_deposits: int = 0
    num_withdrawals: int = 0
    num_days_in_cycle: int = 0
    average_ledger_balance: float = 0.0
    
    # MCA Detection
    mca_payments: list = None
    total_mca_payments: float = 0.0
    
    # Risk Indicators
    nsf_count: int = 0
    overdraft_days: int = 0
    negative_balance_days: list = None
    
    # Raw data
    transactions: list = None
    daily_balances: list = None
    
    def __post_init__(self):
        if self.mca_payments is None:
            self.mca_payments = []
        if self.negative_balance_days is None:
            self.negative_balance_days = []
        if self.transactions is None:
            self.transactions = []
        if self.daily_balances is None:
            self.daily_balances = []


def is_native_pdf(pdf_path: str) -> bool:
    """
    Detect if PDF is native (has extractable text) or scanned (image-based)
    Returns True if native, False if scanned/needs OCR
    """
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Check first 3 pages for text
            text_chars = 0
            pages_checked = min(3, len(pdf.pages))
            
            for i in range(pages_checked):
                page = pdf.pages[i]
                text = page.extract_text() or ""
                text_chars += len(text.strip())
            
            # If we got substantial text, it's native
            avg_chars = text_chars / pages_checked if pages_checked > 0 else 0
            return avg_chars > 100  # At least 100 chars per page on average
            
    except Exception as e:
        print(f"Error checking PDF type: {e}")
        return False


def extract_native_pdf(pdf_path: str) -> str:
    """Extract text from native PDF using pdfplumber"""
    full_text = []
    
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
    
    return "\n\n".join(full_text)


def extract_scanned_pdf(pdf_path: str) -> str:
    """Extract text from scanned PDF using Surya OCR (GPU accelerated)"""
    try:
        from surya.ocr import run_ocr
        from surya.model.detection.model import load_model as load_det_model
        from surya.model.recognition.model import load_model as load_rec_model
        from surya.model.detection.processor import load_processor as load_det_processor
        from surya.model.recognition.processor import load_processor as load_rec_processor
        from PIL import Image
        import fitz
        
        print("Loading Surya OCR models (first run downloads ~2GB)...")
        
        # Load models
        det_model = load_det_model()
        det_processor = load_det_processor()
        rec_model = load_rec_model()
        rec_processor = load_rec_processor()
        
        # Convert PDF pages to images
        doc = fitz.open(pdf_path)
        images = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            # Render at 300 DPI for good OCR quality
            mat = fitz.Matrix(300/72, 300/72)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            images.append(img)
        
        doc.close()
        
        # Run OCR
        print(f"Running OCR on {len(images)} pages...")
        results = run_ocr(
            images,
            [["en"]] * len(images),  # English for all pages
            det_model,
            det_processor,
            rec_model,
            rec_processor
        )
        
        # Extract text from results
        full_text = []
        for page_result in results:
            page_text = []
            for line in page_result.text_lines:
                page_text.append(line.text)
            full_text.append("\n".join(page_text))
        
        return "\n\n".join(full_text)
        
    except ImportError as e:
        print(f"Surya OCR not available: {e}")
        print("Install with: pip install surya-ocr")
        return ""
    except Exception as e:
        print(f"OCR error: {e}")
        return ""


def parse_currency(text: str) -> float:
    """Parse currency string to float"""
    if not text:
        return 0.0
    # Remove $ , and handle negatives
    cleaned = re.sub(r'[,$\s]', '', str(text))
    # Handle parentheses for negatives
    if cleaned.startswith('(') and cleaned.endswith(')'):
        cleaned = '-' + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_bank_of_america(text: str) -> BankStatementData:
    """Parse Bank of America statement text into structured data"""
    data = BankStatementData()
    data.bank_name = "Bank of America"
    
    # Account number
    match = re.search(r'Account\s*(?:number|#)[:.\s]*(\d[\d\s]+\d)', text, re.IGNORECASE)
    if match:
        data.account_number = match.group(1).replace(' ', '')
    
    # Account holder - prefer business names with company suffix
    match = re.search(r'^([A-Z0-9&\.\'\s]+(?:LLC|INC|CORP|CO))\s*$', text, re.MULTILINE)
    if match:
        data.account_holder = match.group(1).strip()
    
    # Statement period
    match = re.search(r'for\s+(\w+\s+\d+,?\s+\d{4})\s+to\s+(\w+\s+\d+,?\s+\d{4})', text, re.IGNORECASE)
    if match:
        data.statement_period_start = match.group(1)
        data.statement_period_end = match.group(2)
    
    # Beginning balance
    match = re.search(r'Beginning\s+balance[^\d$-]*[$]?([\d,.-]+)', text, re.IGNORECASE)
    if match:
        data.beginning_balance = parse_currency(match.group(1))
    
    # Ending balance
    match = re.search(r'Ending\s+balance[^\d$-]*[$]?([-]?[\d,.-]+)', text, re.IGNORECASE)
    if match:
        data.ending_balance = parse_currency(match.group(1))
    
    # Total deposits
    match = re.search(r'(?:Total\s+)?[Dd]eposits\s+and\s+other\s+credits[^\d$]*[$]?([\d,.-]+)', text)
    if match:
        data.total_deposits = parse_currency(match.group(1))
    
    # Total withdrawals  
    match = re.search(r'(?:Total\s+)?[Ww]ithdrawals\s+and\s+other\s+debits[^\d$-]*[$]?([-]?[\d,.-]+)', text)
    if match:
        data.total_withdrawals = abs(parse_currency(match.group(1)))
    
    # Total checks
    match = re.search(r'(?:Total\s+)?[Cc]hecks[^\d$-]*[$]?([-]?[\d,.-]+)', text)
    if match:
        data.total_checks = abs(parse_currency(match.group(1)))
    
    # Service fees
    match = re.search(r'(?:Total\s+)?[Ss]ervice\s+fees[^\d$-]*[$]?([-]?[\d,.-]+)', text)
    if match:
        data.total_fees = abs(parse_currency(match.group(1)))
    
    # Number of deposits
    match = re.search(r'#\s*of\s*deposits/credits:\s*(\d+)', text, re.IGNORECASE)
    if match:
        data.num_deposits = int(match.group(1))
    
    # Number of withdrawals
    match = re.search(r'#\s*of\s*withdrawals/debits:\s*(\d+)', text, re.IGNORECASE)
    if match:
        data.num_withdrawals = int(match.group(1))
    
    # Days in cycle
    match = re.search(r'#\s*of\s*days\s*in\s*cycle:\s*(\d+)', text, re.IGNORECASE)
    if match:
        data.num_days_in_cycle = int(match.group(1))
    
    # Average ledger balance
    match = re.search(r'[Aa]verage\s+ledger\s+balance:\s*[$]?([\d,.-]+)', text)
    if match:
        data.average_ledger_balance = parse_currency(match.group(1))
    
    # Detect MCA payments
    mca_patterns = [
        (r'KAPITUS.*?[$]?([\d,]+\.?\d*)', 'Kapitus'),
        (r'FORWARD\s*FINANCING.*?[$]?([\d,]+\.?\d*)', 'Forward Financing'),
        (r'FAMILY\s*FUNDING.*?[$]?([\d,]+\.?\d*)', 'Family Funding'),
        (r'CREDIBLY.*?[$]?([\d,]+\.?\d*)', 'Credibly'),
        (r'CLEARCO.*?[$]?([\d,]+\.?\d*)', 'Clearco'),
        (r'FUNDBOX.*?[$]?([\d,]+\.?\d*)', 'Fundbox'),
        (r'BLUEVINE.*?[$]?([\d,]+\.?\d*)', 'Bluevine'),
        (r'ONDECK.*?[$]?([\d,]+\.?\d*)', 'OnDeck'),
        (r'RAPID\s*FINANCE.*?[$]?([\d,]+\.?\d*)', 'Rapid Finance'),
        (r'NATIONAL\s*FUNDING.*?[$]?([\d,]+\.?\d*)', 'National Funding'),
    ]
    
    mca_totals = {}
    for pattern, lender in mca_patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            total = sum(parse_currency(m) for m in matches)
            if total > 0:
                mca_totals[lender] = {
                    'count': len(matches),
                    'total': total,
                    'avg_payment': total / len(matches)
                }
    
    data.mca_payments = [{'lender': k, **v} for k, v in mca_totals.items()]
    data.total_mca_payments = sum(v['total'] for v in mca_totals.values())
    
    # Parse daily balances for negative days
    balance_pattern = r'(\d{2}/\d{2})\s+([-]?[\d,]+\.?\d*)'
    daily_section = re.search(r'Daily\s+ledger\s+balances(.*?)(?:Page|\Z)', text, re.DOTALL | re.IGNORECASE)
    if daily_section:
        balances = re.findall(balance_pattern, daily_section.group(1))
        for date, balance in balances:
            bal = parse_currency(balance)
            data.daily_balances.append({'date': date, 'balance': bal})
            if bal < 0:
                data.negative_balance_days.append({'date': date, 'balance': bal})
    
    data.overdraft_days = len(data.negative_balance_days)
    
    # Count NSF/overdraft fees
    nsf_matches = re.findall(r'OVERDRAFT\s+ITEM\s+FEE|NSF|RETURNED\s+ITEM\s+FEE', text, re.IGNORECASE)
    data.nsf_count = len(nsf_matches)
    
    return data


def parse_amex_business(text: str) -> BankStatementData:
    """Parse American Express Business Checking Account Statement"""
    data = BankStatementData()
    data.bank_name = "American Express Business Checking"
    
    # Account number (last 4) - e.g. "Account Ending: *6397"
    match = re.search(r'Account\s+Ending:\s*([*\d]+)', text, re.IGNORECASE)
    if match:
        data.account_number = match.group(1).strip()
    
    # Account holder - "Account Holder: Abundia LC Corp" or company name line
    match = re.search(r'Account\s+Holder:\s*([^\n]+)', text, re.IGNORECASE)
    if match:
        data.account_holder = match.group(1).strip()
    else:
        # Fallback: after "Business Checking Account Statement" and "p. 1/12", next line is company name
        match = re.search(
            r'Business\s+Checking\s+Account\s+Statement\s*\n\s*[^\n]+\s*\n\s*([A-Za-z0-9&\s\.\',]+(?:LLC|CORP|INC))\s*\n',
            text,
            re.IGNORECASE,
        )
        if match:
            data.account_holder = match.group(1).strip()
    
    # Statement period via beginning/ending balance dates
    match_start = re.search(
        r'Beginning\s+Balance\s+as\s+of\s+(\d{2}/\d{2}/\d{4})',
        text,
        re.IGNORECASE,
    )
    match_end = re.search(
        r'Ending\s+Balance\s+as\s+of\s+(\d{2}/\d{2}/\d{4})',
        text,
        re.IGNORECASE,
    )
    if match_start:
        data.statement_period_start = match_start.group(1)
    if match_end:
        data.statement_period_end = match_end.group(1)
    
    # Amounts may be on same line or next line; allow $ and commas
    # Pattern that allows newlines between label and amount
    _amt = r'[\s\n]*\$?\s*(-?[\d,\.]+)'
    # Beginning balance
    match = re.search(
        r'Beginning\s+Balance\s+as\s+of\s+\d{2}/\d{2}/\d{4}' + _amt,
        text,
        re.IGNORECASE,
    )
    if match:
        data.beginning_balance = parse_currency(match.group(1))
    
    # Ending balance
    match = re.search(
        r'Ending\s+Balance\s+as\s+of\s+\d{2}/\d{2}/\d{4}' + _amt,
        text,
        re.IGNORECASE,
    )
    if match:
        data.ending_balance = parse_currency(match.group(1))
    
    # Total credits this period
    match = re.search(
        r'Total\s+Credits\s+This\s+Period' + _amt,
        text,
        re.IGNORECASE,
    )
    if match:
        data.total_deposits = parse_currency(match.group(1))
    
    # Total debits this period (often "-$427,354.33" - minus before $)
    match = re.search(
        r'Total\s+Debits\s+This\s+Period\s*-?\s*\$?\s*([\d,\.]+)',
        text,
        re.IGNORECASE,
    )
    if match:
        data.total_withdrawals = parse_currency(match.group(1))
    
    # NSF/overdraft: only count explicit fee lines, not "NSF" in narrative
    nsf_matches = re.findall(
        r'OVERDRAFT\s+ITEM\s+FEE|NSF\s+FEE|RETURNED\s+ITEM\s+FEE|Non-Sufficient\s+Funds',
        text,
        re.IGNORECASE,
    )
    data.nsf_count = len(nsf_matches)
    
    return data


def parse_txn_bank(text: str) -> BankStatementData:
    """Parse TXN Bank statement text into structured data"""
    data = BankStatementData()
    data.bank_name = "TXN Bank"
    
    # Account number (from "Account Number XXXXXXX0941" or header)
    match = re.search(r'Account\s+Number[:\s]+([Xx\d]+)', text, re.IGNORECASE)
    if not match:
        match = re.search(r'TXN\s+PROFESSIONAL\s*-\s*([Xx\d]+)', text, re.IGNORECASE)
    if match:
        data.account_number = match.group(1).strip()
    
    # Account holder - business name with LLC/INC/etc
    match = re.search(r'^([A-Z0-9&\.\'\s]+(?:LLC|INC|CORP|CO))\s*$', text, re.MULTILINE)
    if match:
        data.account_holder = match.group(1).strip()
    
    # Statement period from Account Summary section (Beginning / Ending balance dates)
    # e.g. "11/01/2025 Beginning Balance" ... "11/30/2025 Ending Balance"
    match_start = re.search(r'(\d{2}/\d{2}/\d{4})\s+Beginning\s+Balance', text, re.IGNORECASE)
    match_end = re.search(r'(\d{2}/\d{2}/\d{4})\s+Ending\s+Balance', text, re.IGNORECASE)
    if match_start:
        data.statement_period_start = match_start.group(1)
    if match_end:
        data.statement_period_end = match_end.group(1)
    
    # Beginning balance
    match = re.search(r'Beginning\s+Balance\s*\$?\s*([\d,.-]+)', text, re.IGNORECASE)
    if match:
        data.beginning_balance = parse_currency(match.group(1))
    
    # Ending balance
    match = re.search(r'Ending\s+Balance\s*\$?\s*([-]?[\d,.-]+)', text, re.IGNORECASE)
    if match:
        data.ending_balance = parse_currency(match.group(1))
    
    # Period credits/deposits: "11 Credit(s) This Period    $198,823.25"
    match = re.search(
        r'(\d+)\s+Credit\(s\)\s+This\s+Period[^\d$-]*\$?([\d,.-]+)',
        text,
        re.IGNORECASE,
    )
    if match:
        data.num_deposits = int(match.group(1))
        data.total_deposits = parse_currency(match.group(2))
    
    # Period debits/withdrawals: "136 Debit(s) This Period   $205,093.30"
    match = re.search(
        r'(\d+)\s+Debit\(s\)\s+This\s+Period[^\d$-]*\$?([-]?[\d,.-]+)',
        text,
        re.IGNORECASE,
    )
    if match:
        data.num_withdrawals = int(match.group(1))
        data.total_withdrawals = abs(parse_currency(match.group(2)))
    
    # Total checks: from "Checks Cleared ... 45 item(s) totaling $122,760.02"
    match = re.search(
        r'Checks\s+Cleared[\s\S]*?(\d+)\s+item\(s\)\s+totaling[^\d$-]*\$?([\d,.-]+)',
        text,
        re.IGNORECASE,
    )
    if match:
        data.total_checks = abs(parse_currency(match.group(2)))
    
    # Daily balances for overdraft/average balance
    # Section header: "Daily Balances"
    balance_section = re.search(
        r'Daily\s+Balances(.*?)(?:Overdraft\s+and\s+Returned\s+Item\s+Fees|Total\s+Overdraft\s+Fees|$)',
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if balance_section:
        balance_pattern = r'(\d{2}/\d{2}/\d{4})\s+([-]?[\d,]+\.?\d*)'
        balances = re.findall(balance_pattern, balance_section.group(1))
        running_total = 0.0
        count = 0
        for date_str, bal_str in balances:
            bal = parse_currency(bal_str)
            data.daily_balances.append({'date': date_str, 'balance': bal})
            running_total += bal
            count += 1
            if bal < 0:
                data.negative_balance_days.append({'date': date_str, 'balance': bal})
        if count > 0:
            data.average_ledger_balance = running_total / count
    
    data.overdraft_days = len(data.negative_balance_days)
    
    # Count NSF/overdraft fees in narrative lines
    nsf_matches = re.findall(
        r'OVERDRAFT\s+ITEM\s+FEE|NSF\s+FEE|RETURNED\s+ITEM\s+FEE',
        text,
        re.IGNORECASE,
    )
    data.nsf_count = len(nsf_matches)
    
    return data


def parse_chase(text: str) -> BankStatementData:
    """Parse Chase Business Complete Checking statement."""
    data = BankStatementData()
    data.bank_name = "Chase"

    # Account number: "Account Number: 000000552696885"
    match = re.search(r'Account\s+Number[:\s]+([0-9X ]+)', text, re.IGNORECASE)
    if match:
        data.account_number = match.group(1).replace(" ", "").strip()

    # Isolate CHECKING SUMMARY block to avoid picking up other \"Ending Balance\" lines.
    # We use the custom markers that pdfplumber extracted in this layout (*start*summary ... *end*summary).
    summary_section = re.search(
        r'\*start\*summary([\s\S]*?)\*end\*summary',
        text,
        re.IGNORECASE,
    )
    summary_text = summary_section.group(1) if summary_section else text

    # Statement period: e.g. "November 01, 2025 through November 28, 2025"
    # Allow for extra spaces/newlines and "through" or "to" or "-" between dates.
    match = re.search(
        r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*(?:through|to|-)\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
        text,
        re.IGNORECASE,
    )
    if not match:
        # Sometimes split across lines: first date on one line, second on next
        match = re.search(
            r'([A-Za-z]+\s+\d{1,2},\s+\d{4})\s*(?:through|to|-)\s*[\r\n]+\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})',
            text,
            re.IGNORECASE,
        )
    if match:
        data.statement_period_start = match.group(1).strip()
        data.statement_period_end = match.group(2).strip()

    # CHECKING SUMMARY block totals
    # Beginning Balance: match the amount on the same line (e.g. \"Beginning Balance $41,275.18\")
    match = re.search(
        r'^Beginning\s+Balance\s+(-?\$?[\d,]+\.\d{2})\s*$',
        summary_text,
        re.MULTILINE | re.IGNORECASE,
    )
    if match:
        data.beginning_balance = parse_currency(match.group(1))

    # Ending Balance: match the amount on the same line (e.g. \"Ending Balance 95 $1,120.84\")
    match = re.search(
        r'^Ending\s+Balance.*?(-?\$?[\d,]+\.\d{2})\s*$',
        summary_text,
        re.MULTILINE | re.IGNORECASE,
    )
    if match:
        data.ending_balance = parse_currency(match.group(1))

    # Deposits and Additions: "Deposits and Additions   18   69,391.34"
    match = re.search(
        r'Deposits\s+and\s+Additions\s+\d+\s+([\d,]+\.\d{2})',
        text,
        re.IGNORECASE,
    )
    if match:
        data.total_deposits = parse_currency(match.group(1))

    # Withdrawals: sum of several lines in summary
    withdrawals_total = 0.0
    for label in [
        r'Checks\s+Paid',
        r'ATM\s*&\s*Debit\s+Card\s+Withdrawals',
        r'Electronic\s+Withdrawals',
        r'Other\s+Withdrawals',
        r'Fees',
    ]:
        m = re.search(label + r'\s+\d+\s+([\d,]+\.\d{2})', text, re.IGNORECASE)
        if m:
            withdrawals_total += parse_currency(m.group(1))
    if withdrawals_total > 0:
        data.total_withdrawals = withdrawals_total

    # Count NSF/overdraft-related fees
    nsf_matches = re.findall(
        r'OVERDRAFT\s+ITEM\s+FEE|NSF\s+FEE|RETURNED\s+ITEM\s+FEE|Insufficient\s+Funds',
        text,
        re.IGNORECASE,
    )
    data.nsf_count = len(nsf_matches)

    return data


def extract_and_parse(pdf_path: str) -> BankStatementData:
    """Main entry point: extract and parse a bank statement PDF"""
    pdf_path = Path(pdf_path)
    
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    
    print(f"\nProcessing: {pdf_path.name}")
    print("-" * 50)
    
    # Step 1: Detect PDF type
    native = is_native_pdf(str(pdf_path))
    print(f"PDF Type: {'Native (digital)' if native else 'Scanned (needs OCR)'}")
    
    # Step 2: Extract text
    if native:
        print("Extracting with pdfplumber (instant)...")
        text = extract_native_pdf(str(pdf_path))
    else:
        print("Extracting with Surya OCR (GPU)...")
        text = extract_scanned_pdf(str(pdf_path))
    
    if not text:
        raise ValueError("Failed to extract text from PDF")
    
    print(f"Extracted {len(text):,} characters")

    # Optionally save raw extracted text next to the PDF for debugging
    try:
        raw_text_path = pdf_path.with_name(pdf_path.stem + "_text.txt")
        with raw_text_path.open("w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    
    # Step 3: Detect bank and parse
    text_lower = text.lower()
    if 'american express' in text_lower and 'business checking account statement' in text_lower:
        print("Detected: American Express Business Checking")
        data = parse_amex_business(text)
    elif 'txn bank' in text_lower:
        print("Detected: TXN Bank")
        data = parse_txn_bank(text)
    elif 'chase' in text_lower:
        print("Detected: Chase")
        data = parse_chase(text)
    elif 'bank of america' in text_lower:
        print("Detected: Bank of America")
        data = parse_bank_of_america(text)
    elif 'wells fargo' in text_lower:
        print("Detected: Wells Fargo (using generic parser)")
        data = parse_bank_of_america(text)  # TODO: Add Wells Fargo parser
    else:
        print("Unknown bank - using generic parser")
        data = parse_bank_of_america(text)
    
    return data


def process_statement(pdf_path: str, output_dir: str = None) -> dict:
    """Process a bank statement and save JSON output"""
    data = extract_and_parse(pdf_path)
    result = asdict(data)
    
    # Print summary
    print("\n" + "=" * 50)
    print("EXTRACTION SUMMARY")
    print("=" * 50)
    print(f"Bank: {data.bank_name}")
    print(f"Account: {data.account_number}")
    print(f"Period: {data.statement_period_start} to {data.statement_period_end}")
    print(f"\nBeginning Balance: ${data.beginning_balance:,.2f}")
    print(f"Ending Balance: ${data.ending_balance:,.2f}")
    print(f"Total Deposits: ${data.total_deposits:,.2f}")
    print(f"Total Withdrawals: ${data.total_withdrawals:,.2f}")
    print(f"Average Balance: ${data.average_ledger_balance:,.2f}")
    
    if data.mca_payments:
        print(f"\n⚠️  MCA POSITIONS DETECTED:")
        for mca in data.mca_payments:
            print(f"   - {mca['lender']}: {mca['count']} payments, ${mca['total']:,.2f} total")
        print(f"   TOTAL MCA BURDEN: ${data.total_mca_payments:,.2f}")
    
    if data.overdraft_days > 0:
        print(f"\n🚨 NEGATIVE BALANCE: {data.overdraft_days} days")
    
    if data.nsf_count > 0:
        print(f"🚨 NSF/OVERDRAFT FEES: {data.nsf_count}")
    
    # Save JSON if output dir specified
    if output_dir:
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        pdf_name = Path(pdf_path).stem
        json_path = output_path / f"{pdf_name}_analysis.json"
        
        with open(json_path, 'w') as f:
            json.dump(result, f, indent=2)
        print(f"\nSaved: {json_path}")
    
    return result


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python extractor.py <pdf_path> [output_dir]")
        print("\nExample:")
        print("  python extractor.py samples/statement.pdf output/")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    
    try:
        result = process_statement(pdf_path, output_dir)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
