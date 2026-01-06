from typing import List, Dict
import re
import io
from PIL import Image
import pytesseract

def extract_text_from_image_bytes(data: bytes) -> str:
    img = Image.open(io.BytesIO(data))
    return pytesseract.image_to_string(img)

def parse_leads_from_text(text: str) -> List[Dict[str, str]]:
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

