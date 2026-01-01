"""
Image OCR import helpers for AgencyVault.

This module extracts text from uploaded images
and converts it into structured lead rows.
"""

from typing import List, Dict
import re
from PIL import Image
import pytesseract


# ============================================================
# OCR
# ============================================================

def extract_text_from_image(image: Image.Image) -> str:
    """
    Run OCR on a PIL Image and return extracted text.
    """
    return pytesseract.image_to_string(image)


# ============================================================
# LEAD PARSING
# ============================================================

def parse_leads_from_text(text: str) -> List[Dict[str, str]]:
    """
    Parse OCR text into lead dictionaries.

    Expected formats (flexible):
    - Name, Phone, Email
    - Name Phone Email
    - Line-by-line blocks
    """

    leads: List[Dict[str, str]] = []

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

