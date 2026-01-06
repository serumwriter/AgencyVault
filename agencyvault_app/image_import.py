from typing import List, Dict
import re
import io

# OCR is OPTIONAL — do not crash worker if missing
try:
    from PIL import Image
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False


def extract_text_from_image_bytes(data: bytes) -> str:
    """
    Safely extract text from image bytes.
    If OCR is not available, return empty string instead of crashing.
    """
    if not OCR_AVAILABLE:
        return ""

    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img)
    except Exception:
        return ""


def parse_pdf_lead_blocks(text: str) -> list[dict]:
    leads = []
    blocks = text.split("Inquiry Id:")

    for block in blocks:
        if "Phone:" not in block:
            continue

        def grab(label):
            for line in block.splitlines():
                if line.lower().startswith(label.lower()):
                    return line.split(":", 1)[1].strip()
            return None

        lead = {
            "first_name": grab("First Name"),
            "last_name": grab("Last Name"),
            "phone": grab("Phone"),
            "email": grab("Email"),
            "birthdate": grab("Date of Birth"),
            "state": grab("State"),
            "coverage_requested": grab("Desired Coverage Amount"),
        }

        # Require phone + at least first or last name
        if lead["phone"] and (lead["first_name"] or lead["last_name"]):
            leads.append(lead)

    return leads
    
def parse_leads_from_text(text: str) -> List[Dict[str, str]]:
    """
    Parse loose OCR text into leads.
    Safe, best-effort only.
    """
    leads: List[Dict[str, str]] = []
    if not text:
        return leads

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    buffer: Dict[str, str] = {}

    phone_re = re.compile(r"(\+?\d[\d\-\(\) ]{7,}\d)")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    for line in lines:
        phone_match = phone_re.search(line)
        email_match = email_re.search(line)

        if not buffer:
            buffer["full_name"] = line
            continue

        if phone_match:
            buffer["phone"] = phone_match.group(1)

        if email_match:
            buffer["email"] = email_match.group(0)

        if "full_name" in buffer and "phone" in buffer:
            leads.append(buffer)
            buffer = {}

    if buffer:
        leads.append(buffer)

    return leads
# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

from pypdf import PdfReader

def extract_text_from_pdf_bytes(data: bytes) -> str:
    """
    Extract text from a PDF file.
    Works for typed PDFs (forms, docs, exports).
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
