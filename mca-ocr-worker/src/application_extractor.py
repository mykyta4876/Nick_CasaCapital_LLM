"""
Application PDF extractor
-------------------------

Parses a broker/application PDF into a minimal dict that can be
mapped onto `ApplicationData` for underwriting.

Usage:
    python src/application_extractor.py samples/application.pdf
"""

from pathlib import Path
import re
from datetime import date
from typing import Dict, Any

import pdfplumber


def _extract_text(pdf_path: str) -> str:
    """Extract text from a (native) PDF using pdfplumber."""
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text.append(text)
    return "\n".join(full_text)


def _parse_float(value: str) -> float:
    if not value:
        return 0.0
    cleaned = re.sub(r"[,$\s]", "", value)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def extract_application_data(pdf_path: str) -> Dict[str, Any]:
    """
    Extract key underwriting fields from an application PDF.

    Returns a dict that can be merged into ApplicationData, e.g.:
      {
        "business_name": "...",
        "time_in_business_years": 3.0,
        "fico_score": 650,
        "industry": "construction",
        "state": "TX",
        "requested_amount": 250000,
        "use_of_funds": "working capital"
      }
    """
    pdf_path = str(pdf_path)
    if not Path(pdf_path).exists():
        raise FileNotFoundError(f"Application PDF not found: {pdf_path}")

    text = _extract_text(pdf_path)
    data: Dict[str, Any] = {}

    # Business name: "Legal Name ABUNDIA LC CORP" or "Legal Business Name: ..."
    match = re.search(
        r"Legal\s+Name\s+([A-Za-z0-9&\s\.\',]+?)(?:\s+Phone|\s+Fax|\s*$)",
        text,
        re.IGNORECASE,
    )
    if match:
        data["business_name"] = match.group(1).strip()
    else:
        match = re.search(
            r"(?:Legal\s+Business\s+Name|Business\s+Name|Company\s+Name)[:\s]+(.+)",
            text,
        )
        if match:
            data["business_name"] = match.group(1).strip()

    # Time in business: from "Business Start Date 2023-06-01" (compute years to today)
    match = re.search(
        r"Business\s+Start\s+Date\s+(\d{4})-(\d{2})-(\d{2})",
        text,
        re.IGNORECASE,
    )
    if match:
        try:
            start = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            delta = date.today() - start
            data["time_in_business_years"] = round(delta.days / 365.25, 1)
        except (ValueError, TypeError):
            pass
    else:
        match = re.search(
            r"Time\s+in\s+Business[^\d]{0,20}([0-9]{1,2}(?:\.[0-9]+)?)",
            text,
            re.IGNORECASE,
        )
        if match:
            try:
                data["time_in_business_years"] = float(match.group(1))
            except ValueError:
                pass

    # FICO: "Est. FICO" or "FICO Score 650" (3-digit number)
    match = re.search(
        r"(?:Est\.?\s*FICO|FICO\s*Score|Credit\s*Score)[^\d]{0,20}([5-8][0-9]{2})",
        text,
        re.IGNORECASE,
    )
    if match:
        try:
            data["fico_score"] = int(match.group(1))
        except ValueError:
            pass

    # State: "State of Incorporation fl" or "State fl" (2-letter, allow lowercase)
    match = re.search(
        r"(?:State\s+of\s+Incorporation|State)[:\s]+([A-Za-z]{2})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        data["state"] = match.group(1).strip().upper()

    # Industry: "Industry logisticts" or "Industry logistics Address"
    match = re.search(
        r"Industry\s+([A-Za-z]+)(?:\s+Address|\s+State|\s*$)",
        text,
        re.IGNORECASE,
    )
    if match:
        data["industry"] = match.group(1).strip()
    else:
        match = re.search(
            r"(?:Industry|Business\s+Type|Nature\s+of\s+Business)[:\s]+(\S+)",
            text,
            re.IGNORECASE,
        )
        if match:
            data["industry"] = match.group(1).strip()

    # Amount Requested: "Amount Requested $ 100000" or "Amount Requested $"
    match = re.search(
        r"Amount\s+Requested\s+\$?\s*([\d,\.]+)",
        text,
        re.IGNORECASE,
    )
    if match:
        data["requested_amount"] = _parse_float(match.group(1))
    else:
        match = re.search(
            r"(?:Requested\s+Amount|Funding\s+Amount|Loan\s+Amount)[^\d$]{0,20}\$?\s*([\d,\.]+)",
            text,
            re.IGNORECASE,
        )
        if match:
            data["requested_amount"] = _parse_float(match.group(1))

    # Use of funds: "Use of Proceeds" (stop before Landlord / next label)
    match = re.search(
        r"(?:Use\s+of\s+Proceeds|Use\s+of\s+Funds|Purpose\s+of\s+Funds)[:\s]*([A-Za-z0-9\s,\.\-/]+?)(?:\s+Landlord|\s+Seasonal|\n|$)",
        text,
        re.IGNORECASE,
    )
    if match:
        val = match.group(1).strip()
        if val and val.lower() not in ("landlord name", "landlord phone", ""):
            data["use_of_funds"] = val

    return data


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python src/application_extractor.py <application.pdf> [output.json]")
        sys.exit(1)

    pdf_arg = sys.argv[1]
    output_arg = sys.argv[2] if len(sys.argv) > 2 else "app_data.json"

    result = extract_application_data(pdf_arg)
    with open(output_arg, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Saved application data to {output_arg}")

