import os
import re
import json
import time
from datetime import datetime
from urllib.parse import urljoin
from io import BytesIO

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore, storage, messaging
from google.cloud.firestore_v1.base_query import FieldFilter
from pypdf import PdfReader
from PIL import Image, ImageFilter, ImageOps
from pdf2image import convert_from_bytes
import pytesseract

BASE_URL = "https://lotterysambadresult.in/"
BUCKET_NAME = "grozip-pro.firebasestorage.app"

DRAW_PAGES = {
    "1 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
    "6 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
    "8 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-8-pm.html",
}

DRAW_CODES = {
    "1 PM": "1PM",
    "6 PM": "6PM",
    "8 PM": "8PM",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
}

firebase_key = os.environ.get("FIREBASE_KEY")

if firebase_key:
    cred = credentials.Certificate(json.loads(firebase_key))
else:
    cred = credentials.Certificate("serviceAccountKey.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

db = firestore.client()
bucket = storage.bucket()


def today_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def detect_file_type(content: bytes):
    if len(content) >= 4 and content[:4] == b"%PDF":
        return "pdf", "application/pdf"
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None


def fetch_page(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return response.text


def fetch_page_with_retry(url: str, retries: int = 3, delay_seconds: int = 10) -> str:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            return fetch_page(url)
        except Exception as e:
            last_error = e
            print(f"[RETRY] page fetch attempt {attempt}/{retries} failed -> {e}")
            if attempt < retries:
                time.sleep(delay_seconds)

    raise last_error


def download_file(url: str):
    response = requests.get(url, headers=HEADERS, timeout=40)
    response.raise_for_status()

    content = response.content
    ext, content_type = detect_file_type(content)

    if ext:
        return content, ext, content_type

    header_type = (response.headers.get("Content-Type") or "").lower()

    if "application/pdf" in header_type or "pdf" in header_type:
        return content, "pdf", "application/pdf"
    if "image/jpeg" in header_type or "image/jpg" in header_type:
        return content, "jpg", "image/jpeg"
    if "image/png" in header_type:
        return content, "png", "image/png"
    if "image/webp" in header_type:
        return content, "webp", "image/webp"

    return content, None, None


def upload_to_storage(date_str, draw_code, content, ext, content_type, kind):
    storage_path = f"results/{date_str}/{draw_code}_{kind}.{ext}"
    blob = bucket.blob(storage_path)
    blob.upload_from_string(content, content_type=content_type)
    blob.make_public()
    return storage_path, blob.public_url


def convert_pdf_to_poster_webp(pdf_bytes: bytes):
    try:
        images = convert_from_bytes(
            pdf_bytes,
            first_page=1,
            last_page=1,
            dpi=250,
        )

        if not images:
            print("[WARN] PDF convert returned no image")
            return None

        buffer = BytesIO()
        images[0].convert("RGB").save(buffer, format="WEBP", quality=95)

        poster_bytes = buffer.getvalue()
        print(f"[INFO] PDF poster generated, size={len(poster_bytes)} bytes")

        if len(poster_bytes) < 50000:
            print("[WARN] Generated poster too small, skipping")
            return None

        return poster_bytes

    except Exception as e:
        print(f"[WARN] PDF to poster conversion failed: {e}")
        return None


def log_sync(success: bool, message: str):
    db.collection("sync_logs").add({
        "job_name": "lottery_sync_github_actions",
        "run_type": "github_actions",
        "success": success,
        "message": message,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


def already_final_synced(date_str: str, draw_code: str) -> bool:
    doc = db.collection("results").document(f"{date_str}_{draw_code}").get()

    if not doc.exists:
        return False

    data = doc.to_dict() or {}

    return bool(data.get("pdf_url")) and data.get("match_ready") is True


def send_result_notification(date_str: str, draw_label: str):
    tokens_snapshot = (
        db.collection("DeviceTokens")
        .where(filter=FieldFilter("is_active", "==", True))
        .where(filter=FieldFilter("notifications_enabled", "==", True))
        .stream()
    )

    tokens = []

    for doc in tokens_snapshot:
        data = doc.to_dict() or {}
        token = data.get("token")
        if token:
            tokens.append(token)

    if not tokens:
        print(f"[INFO] No active enabled device tokens for {draw_label}")
        return

    success_count = 0
    invalid_tokens = []

    for token in tokens:
        try:
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(
                    title="WB Lottery Result – Live",
                    body=f"{draw_label} result published for {date_str}",
                ),
                data={
                    "type": "result_published",
                    "date": date_str,
                    "time": draw_label,
                    "draw_code": DRAW_CODES.get(draw_label, ""),
                },
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        channel_id="wb_lottery_channel",
                    ),
                ),
            )

            messaging.send(message)
            success_count += 1

        except Exception as e:
            print(f"[WARN] Notification failed: {e}")
            invalid_tokens.append(token)

    for token in invalid_tokens:
        db.collection("DeviceTokens").document(token).set({
            "is_active": False,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    print(f"[INFO] Notification sent to {success_count} device(s) for {draw_label}")


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = []

        for page in reader.pages:
            texts.append(page.extract_text() or "")

        return "\n".join(texts)

    except Exception as e:
        print(f"[WARN] PDF text extract failed: {e}")
        return ""


def prepare_image_for_ocr(image: Image.Image, threshold_value: int = 145) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size

    image = image.resize((width * 3, height * 3))
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.point(lambda x: 0 if x < threshold_value else 255)

    return image


def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    try:
        original_image = Image.open(BytesIO(image_bytes)).convert("RGB")
        ocr_outputs = []

        for threshold in [115, 130, 145, 160, 175]:
            try:
                processed_image = prepare_image_for_ocr(
                    original_image,
                    threshold_value=threshold,
                )

                text = pytesseract.image_to_string(
                    processed_image,
                    config=(
                        "--oem 3 --psm 6 "
                        "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789₹/-:., "
                    ),
                )

                if text.strip():
                    ocr_outputs.append(text)

            except Exception as e:
                print(f"[WARN] OCR threshold {threshold} failed: {e}")

        if not ocr_outputs:
            fallback_text = pytesseract.image_to_string(
                original_image,
                config="--oem 3 --psm 6",
            )
            ocr_outputs.append(fallback_text)

        final_text = "\n".join(ocr_outputs)

        print("[OCR PREVIEW]")
        print(final_text[:700])

        return final_text

    except Exception as e:
        print(f"[WARN] OCR image extract failed: {e}")
        return ""


def clean_number_list(numbers):
    clean = []
    seen = set()

    for n in numbers:
        n = str(n).strip()

        if not n:
            continue

        if n not in seen:
            clean.append(n)
            seen.add(n)

    return clean


def empty_prize_numbers():
    return {
        "first_prize_series": "",
        "first_prize_number": "",
        "consolation_number": "",
        "second_prize": [],
        "third_prize": [],
        "fourth_prize": [],
        "fifth_prize": [],
        "prize_amounts": {
            "first": "₹1 Crore",
            "consolation": "₹1000",
            "second": "₹10000",
            "third": "₹500",
            "fourth": "₹250",
            "fifth": "₹120",
        },
        "match_ready": False,
        "parsed_source": "none",
        "parsed_at": firestore.SERVER_TIMESTAMP,
    }


def normalize_ocr_text_for_parsing(raw_text: str) -> str:
    text = raw_text or ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[^\w₹/\-:.,\n ]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def fix_digits_only_text(text: str) -> str:
    if not text:
        return ""

    replacements = {
        "O": "0", "o": "0", "Q": "0", "D": "0",
        "I": "1", "l": "1", "|": "1", "!": "1",
        "S": "5", "s": "5",
        "Z": "2", "z": "2",
        "B": "8",
        "G": "6",
    }

    for wrong, right in replacements.items():
        text = text.replace(wrong, right)

    return text


def extract_five_digit_numbers(text: str):
    if not text:
        return []

    text = fix_digits_only_text(text.upper())

    results = []
    results.extend(re.findall(r"\b\d{5}\b", text))

    spaced = re.findall(r"\b(\d)\s+(\d)\s+(\d)\s+(\d)\s+(\d)\b", text)
    for group in spaced:
        results.append("".join(group))

    long_numbers = re.findall(r"\d{6,}", text)
    for block in long_numbers:
        for i in range(0, len(block) - 4):
            part = block[i:i + 5]
            if len(part) == 5:
                results.append(part)

    filtered = []
    for n in results:
        if n.startswith("202"):
            continue
        if n in ("00000", "11111", "22222", "99999"):
            continue
        filtered.append(n)

    return clean_number_list(filtered)


def extract_four_digit_numbers(text: str):
    if not text:
        return []

    text = fix_digits_only_text(text.upper())

    results = []
    results.extend(re.findall(r"\b\d{4}\b", text))

    spaced = re.findall(r"\b(\d)\s+(\d)\s+(\d)\s+(\d)\b", text)
    for group in spaced:
        results.append("".join(group))

    long_numbers = re.findall(r"\d{5,}", text)
    for block in long_numbers:
        for i in range(0, len(block) - 3):
            part = block[i:i + 4]
            if len(part) == 4:
                results.append(part)

    filtered = []
    for n in results:
        if n.startswith("202"):
            continue
        if n in ("0000", "1111", "2222", "9999"):
            continue
        filtered.append(n)

    return clean_number_list(filtered)


def parse_prize_numbers(raw_text: str, source: str):
    original_text = normalize_ocr_text_for_parsing(raw_text or "")
    parsed = empty_prize_numbers()
    parsed["parsed_source"] = source

    if not original_text:
        parsed["parse_confidence"] = 0
        parsed["needs_review"] = True
        return parsed

    first_series, first_number = extract_first_prize(original_text)

    parsed["first_prize_series"] = first_series
    parsed["first_prize_number"] = first_number
    parsed["consolation_number"] = first_number

    blocks = split_text_by_prize_blocks(original_text)

    second_numbers = extract_five_digit_numbers(blocks["second"])
    third_numbers = extract_four_digit_numbers(blocks["third"])
    fourth_numbers = extract_four_digit_numbers(blocks["fourth"])
    fifth_numbers = extract_four_digit_numbers(blocks["fifth"])

    all_five = extract_five_digit_numbers(original_text)
    all_four = extract_four_digit_numbers(original_text)

    if first_number:
        all_five = [n for n in all_five if n != first_number]

    if len(second_numbers) < 10:
        second_numbers = clean_number_list(second_numbers + all_five)[:10]

    if len(third_numbers) < 10:
        third_numbers = clean_number_list(third_numbers + all_four[:10])[:10]

    if len(fourth_numbers) < 10:
        fourth_numbers = clean_number_list(fourth_numbers + all_four[10:20])[:10]

    if len(fifth_numbers) < 30:
        fifth_numbers = clean_number_list(fifth_numbers + all_four[20:])

    parsed["second_prize"] = clean_number_list(second_numbers)
    parsed["third_prize"] = clean_number_list(third_numbers)
    parsed["fourth_prize"] = clean_number_list(fourth_numbers)
    parsed["fifth_prize"] = clean_number_list(fifth_numbers)

    confidence = 0

    if first_series and first_number:
        confidence += 30
    if len(parsed["second_prize"]) >= 5:
        confidence += 20
    if len(parsed["third_prize"]) >= 5:
        confidence += 15
    if len(parsed["fourth_prize"]) >= 5:
        confidence += 15
    if len(parsed["fifth_prize"]) >= 30:
        confidence += 20

    parsed["parse_confidence"] = confidence
    parsed["needs_review"] = confidence < 65

    parsed["match_ready"] = bool(
        confidence >= 65
        and parsed["first_prize_number"]
        and (
            parsed["second_prize"]
            or parsed["third_prize"]
            or parsed["fourth_prize"]
            or parsed["fifth_prize"]
        )
    )

    return parsed
