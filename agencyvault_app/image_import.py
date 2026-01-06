import re
import io
from typing import List, Dict

# ============================================================
# OPTIONAL OCR (safe if missing)
# ============================================================

try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False


# ============================================================
# PDF SUPPORT
# ============================================================

from pypdf import PdfReader


# ============================================================
# RAW TEXT EXTRACTION
# ============================================================

def extract_text_from_image_bytes(data: bytes) -> str:
    """
    Extract text from image bytes using OCR.
    Safe: returns empty string if OCR not available.
    """
    if not OCR_AVAILABLE:
        return ""

    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """
    Extract text from typed PDFs.
    """
    text_chunks = []
    reader = PdfReader(io.BytesIO(data))

    for page in reader.pages:
        try:
            txt = page.extract_text()
            if txt:
                text_chunks.append(txt)
        except Exception:
            continue

    return "\n".join(text_chunks)


# ============================================================
# ONE NORMALIZER (CSV + PDF + IMAGE + DOC)
# ============================================================

def normalize_to_leads(data) -> List[Dict[str, str]]:
    """
    Accepts:
      - list[dict]  -> CSV rows
      - str         -> raw text (PDF / OCR / Google Docs)

    Returns:
      - list of normalized lead dictionaries
    """

    leads: List[Dict[str, str]] = []

    # --------------------------------------------------------
    # CASE 1: CSV
    # --------------------------------------------------------
    if isinstance(data, list):
        for row in data:
            clean = {
                k.strip().lower(): (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
                if k
            }

            full_name = (
                clean.get("full name")
                or f"{clean.get('first name','')} {clean.get('last name','')}".strip()
            )

            lead = {
                "full name": full_name or None,
                "phone": (
                    clean.get("phone")
                    or clean.get("phone number")
                    or clean.get("cell")
                    or clean.get("cell phone")
                    or clean.get("mobile")
                ),
                "email": clean.get("email"),
                "state": clean.get("state"),
                "dob": clean.get("dob") or clean.get("date of birth"),
                "coverage amount": clean.get("coverage amount"),
                "coverage type": clean.get("coverage type"),
                "source": clean.get("source"),
                "reference": clean.get("lead id") or clean.get("reference"),
            }

            leads.append(lead)

        return leads

    # --------------------------------------------------------
    # CASE 2: TEXT (PDF / IMAGE / DOC)
    # --------------------------------------------------------
    current: Dict[str, str] = {}
    lines = [l.strip() for l in data.splitlines() if l.strip()]

    for line in lines:
        low = line.lower()

        # Name
        if low.startswith("name") or "full name" in low:
            current["full name"] = line.split(":", 1)[-1].strip()

        # Phone
        elif re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", line):
            current["phone"] = line

        # Email
        elif "@" in line and "." in line:
            current["email"] = line

        # DOB
        elif "birth" in low or "dob" in low:
            current["dob"] = line.split(":", 1)[-1].strip()

        # Coverage
        elif "$" in line or "coverage" in low:
            current["coverage amount"] = line

        # US State
        elif low.startswith("state"):
            current["state"] = line.split(":", 1)[-1].strip()

        # Finalize when enough info collected
        if len(current) >= 3:
            leads.append(current)
            current = {}

    if current:
        leads.append(current)

    return leads
