from PIL import Image
import pytesseract
import re


def extract_text_from_image(file_bytes):
    img = Image.open(file_bytes)
    return pytesseract.image_to_string(img)


def parse_leads_from_text(text):
    leads = []
    lines = text.splitlines()

    current = {}

    for line in lines:
        phone_match = re.search(r"(\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})", line)
        email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", line)

        if phone_match:
            if current:
                leads.append(current)
                current = {}
            current["phone"] = phone_match.group(1)

        if email_match:
            current["email"] = email_match.group(0)

        if not current.get("name") and len(line.split()) >= 2:
            current["name"] = line.strip()

    if current:
        leads.append(current)

    return leads
