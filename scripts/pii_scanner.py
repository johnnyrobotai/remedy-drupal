#!/usr/bin/env python3
"""Scan downloaded ELAC files for PII (Personally Identifiable Information).

Scans PDFs, DOCX, XLSX, PPTX, DOC, XLS, and TXT files for patterns
that may indicate PII: SSNs, phone numbers, emails, student IDs,
dates of birth, financial info, etc.
"""

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path

import fitz  # pymupdf
import docx
import openpyxl
from pptx import Presentation

SOFFICE = "/opt/homebrew/bin/soffice"


DOWNLOADS_DIR = Path(
    "/Users/johnnyrobot/Library/CloudStorage/"
    "OneDrive-LosAngelesCommunityCollegeDistrict/"
    "project_remedy/exports/ELAC/downloads"
)

OUTPUT_DIR = Path(
    "/Users/johnnyrobot/Library/CloudStorage/"
    "OneDrive-LosAngelesCommunityCollegeDistrict/"
    "project_remedy/exports/ELAC/flagged"
)


# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------

PII_PATTERNS = {
    "SSN": re.compile(
        r"""
        (?<!\d)                     # not preceded by digit
        (?!000|666|9\d{2})          # invalid SSN prefixes
        \d{3}                       # area number
        [-\s]                       # separator
        (?!00)\d{2}                 # group number
        [-\s]                       # separator
        (?!0000)\d{4}              # serial number
        (?!\d)                      # not followed by digit
        """,
        re.VERBOSE,
    ),
    "SSN_no_dashes": re.compile(
        r"""
        (?<!\d)
        (?!000|666|9\d{2})
        \d{3}
        (?!00)\d{2}
        (?!0000)\d{4}
        (?!\d)
        """,
        re.VERBOSE,
    ),
    "phone_number": re.compile(
        r"""
        (?<!\d)
        (?:\+?1[-.\s]?)?           # optional country code
        \(?[2-9]\d{2}\)?           # area code
        [-.\s]?
        [2-9]\d{2}                 # exchange
        [-.\s]?
        \d{4}                      # subscriber
        (?!\d)
        """,
        re.VERBOSE,
    ),
    "email": re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    ),
    "date_of_birth": re.compile(
        r"""
        (?i)
        (?:date\s+of\s+birth|DOB|d\.o\.b\.?|birth\s*date)
        \s*[:\-]?\s*
        (\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})
        """,
        re.VERBOSE,
    ),
    "student_id": re.compile(
        r"""
        (?i)
        (?:student\s*(?:id|#|number|no\.?)|SID|ID\s*#|ID\s*number)
        \s*[:\-]?\s*
        (\d{7,10})
        """,
        re.VERBOSE,
    ),
    "employee_id": re.compile(
        r"""
        (?i)
        (?:employee\s*(?:id|#|number|no\.?)|EID|emp\s*(?:id|#))
        \s*[:\-]?\s*
        (\d{5,10})
        """,
        re.VERBOSE,
    ),
    "credit_card": re.compile(
        r"""
        (?<!\d)
        (?:
          4\d{3}|                   # Visa
          5[1-5]\d{2}|             # Mastercard
          3[47]\d{2}|              # Amex
          6(?:011|5\d{2})          # Discover
        )
        [-\s]?
        \d{4}[-\s]?\d{4}[-\s]?\d{4}
        (?!\d)
        """,
        re.VERBOSE,
    ),
    "bank_account": re.compile(
        r"""
        (?i)
        (?:account\s*(?:#|number|no\.?)|acct\s*(?:#|no\.?))
        \s*[:\-]?\s*
        (\d{8,17})
        """,
        re.VERBOSE,
    ),
    "routing_number": re.compile(
        r"""
        (?i)
        (?:routing\s*(?:#|number|no\.?)|ABA|RTN)
        \s*[:\-]?\s*
        (\d{9})
        """,
        re.VERBOSE,
    ),
    "drivers_license": re.compile(
        r"""
        (?i)
        (?:driver'?s?\s*(?:license|lic\.?)|DL)\s*(?:#|number|no\.?)
        \s*[:\-]?\s*
        ([A-Z]?\d{7,8})
        """,
        re.VERBOSE,
    ),
    "passport": re.compile(
        r"""
        (?i)
        passport\s*(?:#|number|no\.?)
        \s*[:\-]?\s*
        ([A-Z]?\d{6,9})
        """,
        re.VERBOSE,
    ),
}

# Patterns to exclude (common false positives)
FALSE_POSITIVE_EMAILS = re.compile(
    r"(?i)"
    r"(?:example\.com|test\.com|placeholder|noreply|no-reply|"
    r"@laccd\.edu|@elac\.edu|@email\.example)"
)

FALSE_POSITIVE_PHONES = re.compile(
    r"(?:000[-.]?000[-.]?0000|123[-.]?456[-.]?7890|555[-.]?\d{3}[-.]?\d{4})"
)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_pdf_text(path: Path) -> str:
    """Extract text from PDF using pymupdf."""
    text_parts = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            text_parts.append(page.get_text())
    return "\n".join(text_parts)


def extract_docx_text(path: Path) -> str:
    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs)


def extract_xlsx_text(path: Path) -> str:
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            row_text = " ".join(str(c) for c in row if c is not None)
            if row_text.strip():
                parts.append(row_text)
    wb.close()
    return "\n".join(parts)


def extract_pptx_text(path: Path) -> str:
    prs = Presentation(str(path))
    parts = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
    return "\n".join(parts)


def extract_txt_text(path: Path) -> str:
    return path.read_text(errors="replace")


def _convert_with_libreoffice(path: Path, target_ext: str) -> Path:
    """Convert a file using LibreOffice and return the converted path."""
    fmt_map = {".docx": "docx", ".xlsx": "xlsx"}
    out_fmt = fmt_map[target_ext]
    tmpdir = tempfile.mkdtemp(prefix="pii_convert_")
    subprocess.run(
        [SOFFICE, "--headless", "--convert-to", out_fmt, "--outdir", tmpdir, str(path)],
        capture_output=True, timeout=60,
    )
    converted = Path(tmpdir) / (path.stem + target_ext)
    if not converted.exists():
        raise FileNotFoundError(f"LibreOffice conversion failed for {path.name}")
    return converted


def extract_doc_text(path: Path) -> str:
    """Convert .doc → .docx via LibreOffice, then extract text."""
    converted = _convert_with_libreoffice(path, ".docx")
    try:
        return extract_docx_text(converted)
    finally:
        converted.unlink(missing_ok=True)
        converted.parent.rmdir()


def extract_xls_text(path: Path) -> str:
    """Convert .xls → .xlsx via LibreOffice, then extract text."""
    converted = _convert_with_libreoffice(path, ".xlsx")
    try:
        return extract_xlsx_text(converted)
    finally:
        converted.unlink(missing_ok=True)
        converted.parent.rmdir()


EXTRACTORS = {
    ".pdf": extract_pdf_text,
    ".docx": extract_docx_text,
    ".xlsx": extract_xlsx_text,
    ".pptx": extract_pptx_text,
    ".txt": extract_txt_text,
    ".doc": extract_doc_text,
    ".xls": extract_xls_text,
}


# ---------------------------------------------------------------------------
# PII scanning
# ---------------------------------------------------------------------------

@dataclass
class PIIMatch:
    pattern_name: str
    matched_text: str
    context: str  # surrounding text for review


@dataclass
class FileResult:
    filepath: str
    filename: str
    filetype: str
    matches: list[PIIMatch] = field(default_factory=list)
    error: str | None = None


def get_context(text: str, start: int, end: int, window: int = 60) -> str:
    """Get surrounding context for a match, redacting the actual match."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    context = text[ctx_start:ctx_end]
    # Clean up whitespace
    context = re.sub(r"\s+", " ", context).strip()
    return context


def is_false_positive(pattern_name: str, match_text: str, context: str) -> bool:
    """Filter out common false positives."""
    if pattern_name == "email":
        if FALSE_POSITIVE_EMAILS.search(match_text):
            return True
        # Institutional emails are not PII in this context
        if match_text.endswith((".edu", ".gov")):
            return True

    if pattern_name == "phone_number":
        if FALSE_POSITIVE_PHONES.search(match_text):
            return True
        # Published office/department phone numbers aren't PII
        ctx_lower = context.lower()
        if any(w in ctx_lower for w in [
            "office", "department", "fax", "tel:", "phone:",
            "contact us", "call us", "main number",
        ]):
            return True

    if pattern_name == "SSN_no_dashes":
        # Very high false positive rate for 9-digit numbers without dashes
        ctx_lower = context.lower()
        if not any(w in ctx_lower for w in [
            "ssn", "social security", "soc sec", "ss#", "ss #",
        ]):
            return True

    return False


def redact(text: str) -> str:
    """Partially redact a PII value for safe reporting."""
    if len(text) <= 4:
        return "****"
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def scan_text(text: str) -> list[PIIMatch]:
    """Scan text for all PII patterns."""
    matches = []
    for name, pattern in PII_PATTERNS.items():
        for m in pattern.finditer(text):
            match_text = m.group(0).strip()
            context = get_context(text, m.start(), m.end())
            if is_false_positive(name, match_text, context):
                continue
            matches.append(PIIMatch(
                pattern_name=name,
                matched_text=redact(match_text),
                context=context,
            ))
    return matches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scan_all_files():
    results: list[FileResult] = []
    flagged: list[FileResult] = []
    errors: list[FileResult] = []

    # Collect all files
    all_files = []
    for subdir in sorted(DOWNLOADS_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        ext = "." + subdir.name
        for f in sorted(subdir.iterdir()):
            if f.is_file():
                all_files.append((f, ext))

    total = len(all_files)
    print(f"Scanning {total} files for PII...\n")

    for i, (filepath, ext) in enumerate(all_files, 1):
        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] Processing {filepath.name}...")

        result = FileResult(
            filepath=str(filepath),
            filename=filepath.name,
            filetype=ext,
        )

        extractor = EXTRACTORS.get(ext)
        if extractor is None:
            result.error = f"No extractor for {ext}"
            errors.append(result)
            results.append(result)
            continue

        try:
            text = extractor(filepath)
            if not text.strip():
                # Likely scanned image PDF
                result.error = "No extractable text (possibly scanned image)"
                errors.append(result)
                results.append(result)
                continue
            pii_matches = scan_text(text)
            result.matches = pii_matches
            if pii_matches:
                flagged.append(result)
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
            errors.append(result)

        results.append(result)

    # -----------------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------------
    print(f"\n{'='*70}")
    print(f"PII SCAN RESULTS")
    print(f"{'='*70}")
    print(f"Total files scanned:  {total}")
    print(f"Files with PII found: {len(flagged)}")
    print(f"Files with errors:    {len(errors)}")
    print(f"Clean files:          {total - len(flagged) - len(errors)}")
    print()

    if flagged:
        print(f"\n{'='*70}")
        print("FLAGGED FILES (potential PII detected)")
        print(f"{'='*70}\n")

        # Group by PII type
        by_type: dict[str, list] = {}
        for r in flagged:
            for m in r.matches:
                by_type.setdefault(m.pattern_name, []).append(
                    (r.filename, m.matched_text, m.context)
                )

        for pii_type, items in sorted(by_type.items()):
            print(f"\n--- {pii_type} ({len(items)} matches) ---")
            for fname, val, ctx in items[:20]:  # limit output
                print(f"  File: {fname}")
                print(f"  Value: {val}")
                print(f"  Context: ...{ctx}...")
                print()
            if len(items) > 20:
                print(f"  ... and {len(items) - 20} more\n")

    # Save detailed results as JSON
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / "pii_scan_report.json"

    report = {
        "summary": {
            "total_files": total,
            "flagged_files": len(flagged),
            "error_files": len(errors),
            "clean_files": total - len(flagged) - len(errors),
        },
        "flagged": [
            {
                "filename": r.filename,
                "filepath": r.filepath,
                "filetype": r.filetype,
                "matches": [asdict(m) for m in r.matches],
            }
            for r in flagged
        ],
        "errors": [
            {
                "filename": r.filename,
                "filepath": r.filepath,
                "filetype": r.filetype,
                "error": r.error,
            }
            for r in errors
        ],
    }

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nDetailed report saved to: {report_path}")

    # Also save a CSV summary of flagged files
    csv_path = OUTPUT_DIR / "pii_flagged_files.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["filename", "filetype", "pii_type", "redacted_value", "context"])
        for r in flagged:
            for m in r.matches:
                writer.writerow([r.filename, r.filetype, m.pattern_name, m.matched_text, m.context])
    print(f"Flagged files CSV:    {csv_path}")


if __name__ == "__main__":
    scan_all_files()
