# MCA Bank Statement Analyzer & Underwriting Engine

Automated bank statement analysis and underwriting for MCA (Merchant Cash Advance) deals.

## Features

- **PDF Text Extraction**: Native PDF extraction (pdfplumber) + OCR fallback (Surya on RTX 5090)
- **MoneyThumb-Compatible Output**: Matches MoneyThumb's exact format and metrics
- **MCA Lender Detection**: Automatically identifies 35+ MCA lenders from transaction descriptions
- **Configurable Underwriting Rules**: All thresholds, buy rates, and programs editable via JSON
- **Auto-Decision Engine**: AUTO_APPROVE / MANUAL_REVIEW / AUTO_DECLINE based on your rules

## Project Structure

```
C:\mca-ocr-worker\
├── config\
│   └── underwriting_rules.json    # ⚙️ ALL RULES - EDIT THIS FILE
├── src\
│   ├── moneythumb_extractor.py    # Bank statement parser
│   └── underwriting_engine.py     # Underwriting rules engine
├── samples\                        # Drop PDFs/CSVs here to process
├── output\                         # Analysis results go here
├── docs\
│   └── examples\                   # Sample outputs
├── venv\                           # Python virtual environment
└── README.md
```

## Installation

### Prerequisites
- Windows 10/11
- Python 3.11+
- NVIDIA GPU (RTX 5090 recommended for OCR)

### Setup

```powershell
# 1. Navigate to project
cd C:\mca-ocr-worker

# 2. Create virtual environment
python -m venv venv

# 3. Activate it
.\venv\Scripts\activate

# 4. Install dependencies
pip install pdfplumber pymupdf pillow openpyxl

# 5. Install PyTorch with CUDA (for OCR)
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128

# 6. Install Surya OCR
pip install surya-ocr
```

### Optional: Gmail + Flask service

```powershell
# 7. Install Gmail + API server dependencies
pip install flask google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Then create a Google Cloud project, enable the Gmail API, and download `credentials.json`
into the `mca-ocr-worker` folder. The first Gmail call will open a browser to authorize.

## Usage

### 1. Parse Bank Statements (MoneyThumb CSV Export)

```powershell
cd C:\mca-ocr-worker
.\venv\Scripts\activate

python src\moneythumb_extractor.py samples\transactions.csv output\
```

**Output:**
- `output\transactions_analysis.xlsx` - MoneyThumb-format Excel
- `output\transactions_analysis.json` - Structured JSON for API/database

### 2. Run Underwriting

```powershell
python src\underwriting_engine.py output\transactions_analysis.json
```

**Output:**
```
======================================================================
UNDERWRITING DECISION
======================================================================

✅ DECISION: AUTO_APPROVE
   Risk Score: 78/100
   Position: 5

💰 AVAILABLE OFFERS:

   1. High Risk (Position 5) ⭐ RECOMMENDED
      Max Funding:    $  300,000.00
      Term:           3.0 months
      Buy Rate:       1.33
      Sell Rate:      1.42
      Daily Payment:  $    4,733.33
      Commission:     $   21,000.00 (7.0%)
======================================================================
```

### 3. Full Pipeline (PDF → Analysis → Underwriting)

```powershell
# Single PDF → Analysis → Underwriting
python src\pipeline.py samples\bank_statement.pdf

# Folder of PDFs (multiple months/statements)
python src\pipeline.py samples\
```

### 3.1. How to verify the pipeline output

You can sanity‑check parsing and underwriting in two ways:

- **Check extraction only (no underwriting)**  

  ```powershell
  # See what the extractor thinks for a single PDF
  python src\verify_statement.py "samples\some_statement.pdf"
  ```

  This prints a checklist (bank name, account, period, beginning/ending balance,
  total credits/debits, NSF count, etc.) that you can compare to the statement’s
  first page / summary block.

- **Check full end‑to‑end underwriting**  

  ```powershell
  # Single statement + application JSON
  python src\pipeline.py samples\some_statement.pdf app_data.json

  # Folder of statements + application PDF + JSON
  python src\pipeline.py samples\ output\ samples\USC_App_ONE_STOP_PLASTERING_INC.pdf app_data.json
  ```

  Then inspect:

  - `output\<statement_stem>_analysis.json` (per‑PDF extraction).
  - `output\combined_analysis.json` (for batch runs).
  - `output\combined_underwriting.json` or `output\<slug>\underwriting.json`
    (final decision, offers, and stips).

### 4. Gmail → PDFs → Underwriting (automated)

#### One‑time Gmail setup

1. Enable Gmail API and create OAuth client in Google Cloud.
2. Download the OAuth client JSON and save it as `credentials.json` in the `mca-ocr-worker` folder.
3. In `src\gmail_fetcher.py`, the scope is:

```python
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
```

Delete `token.json` if you change scopes; the next run will ask you to re‑authorize.

#### Option A: Manual pull of PDFs from Gmail

```powershell
# From mca-ocr-worker (venv active)
python src\gmail_fetcher.py --query "has:attachment filename:pdf" --max-results 20
```

This saves PDFs into `samples\` and writes `email_metadata.json` into
`casa-capital\deals\<slug>\` if that tree exists.

#### Option B: Always-on underwriting service (Flask)

```powershell
# Start the Flask service (runs on http://127.0.0.1:5000)
python src\uw_service.py

# In another terminal, trigger processing of unread emails
curl -X POST http://127.0.0.1:5000/tasks/process-unread
```

For each unread Gmail with PDF attachments, the service:

- Downloads PDFs into `samples\`.
- Classifies statement vs application PDFs by filename.
- Runs the underwriting pipeline (batch of statements + application data).
- Saves per‑email results under `output\<subject_slug>\underwriting.json`.
- Marks the email as read in Gmail.

#### Option C: Background worker (check every 10 seconds)

Instead of calling the HTTP endpoint, you can run the simple worker:

```powershell
python src\email_worker.py
```

This loops forever, calling the same unread‑email logic every 10 seconds so
new statement/application emails are processed automatically.

## Configuration

### Edit `config\underwriting_rules.json` to adjust:

#### Auto-Decline Triggers
```json
"auto_decline_triggers": {
  "max_negative_days": 5,           // 5+ negative days = auto decline
  "max_nsf_count": 10,              // 10+ NSFs = auto decline
  "min_monthly_deposits": 20000,    // Below $20K = auto decline
  "max_mca_withhold_percent": 0.50, // 50%+ withhold = auto decline
  "max_existing_positions": 6       // 6+ positions = auto decline
}
```

#### Program Requirements
```json
"high_risk": {
  "enabled": true,
  "min_time_in_business_years": 1,
  "min_fico": 550,
  "min_deposit_days_per_month": 5,
  "min_monthly_deposits": 40000,
  "max_negative_days": 4,
  "positions": {
    "1": {"max_term_months": 7, "max_funding": 300000},
    "2": {"max_term_months": 6, "max_funding": 500000},
    ...
  }
}
```

#### Buy Rates
```json
"buy_rates": {
  "1": {"2": 1.28, "3": 1.29, "4": 1.30, "5": 1.31, "6": 1.32, "7": 1.33},
  "2": {"2": 1.29, "3": 1.30, "4": 1.31, "5": 1.32, "6": 1.33}
}
```
*Position 1, 4-month term = 1.30 buy rate*

#### Commission Tiers
```json
"commission_tiers": {
  "high_risk": [
    {"upsell": 0.00, "commission": 0.01},
    {"upsell": 0.02, "commission": 0.05},
    {"upsell": 0.04, "commission": 0.12}
  ]
}
```

## MCA Lenders Detected

The system automatically identifies these lenders from ACH descriptions:

| Lender | Patterns Matched |
|--------|------------------|
| Kapitus | KAPITUS |
| Forward Financing | FORWARD FINANCING |
| Intuit Financing | INTUIT FINANCING, QBC_PMTS INTUIT |
| Billd Exchange | BILLD EXCHANGE, BILLD |
| Cashera Private | CASHERA PRIVATE, CASHERA |
| OnDeck | ONDECK |
| Fundbox | FUNDBOX |
| Bluevine | BLUEVINE |
| Shopify Capital | SHOPIFY CAPITAL |
| Square Capital | SQUARE CAPITAL |
| ... | 35+ total lenders |

## Output Formats

### MoneyThumb-Compatible Excel Sheets

| Sheet | Description |
|-------|-------------|
| Revenue Statistics | Monthly/Annual revenue, expenses, profit |
| MCA Transactions | Individual MCA hits with lender identified |
| Monthly MCA | MCA grouped by month + lender |
| Credit Transactions | All deposits |
| True Credit Transactions | Real revenue only |
| Non-True Credit Transactions | Loans, transfers, owner injections |
| NSF Transactions | Bounced items |
| Incoming/Outgoing Transfers | Money movements |
| Large Transactions | > $1,000 |
| Repeating Transactions | Recurring patterns |

### JSON Structure

```json
{
  "revenue_statistics": {
    "revenue_monthly": 1867931.60,
    "true_revenue_monthly": 1715431.60,
    "expenses_monthly": 2125718.04,
    "mca_withhold_percent": 0.043
  },
  "mca_transactions": [...],
  "nsf_transactions": [...],
  "credit_transactions": [...]
}
```

## Deploy on GCP VM

See **[DEPLOY_GCP.md](DEPLOY_GCP.md)** for step-by-step instructions to run the Flask app and email worker on a Google Cloud VM (Ubuntu, gunicorn, systemd, optional nginx).

## Roadmap

- [ ] Direct PDF parsing (skip MoneyThumb export)
- [ ] API endpoint for real-time underwriting
- [ ] Integration with CRM/LendSaaS
- [ ] Broker portal for deal submission
- [ ] Auto-generate offer letters

## License

Proprietary - Casa Capital Internal Use Only
