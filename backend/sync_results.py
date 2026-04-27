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
from PIL import Image
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

firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

db = firestore.client()
bucket = storage.bucket()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def today_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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


def fetch_page_with_retry(url: str, retries: int = 3, delay_seconds: int = 20) -> str:
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
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()

    content = response.content
    ext, content_type = detect_file_type(content)

    if ext:
        return content, ext, content_type

    header_type = (response.headers.get("Content-Type") or "").lower()

    if "application/pdf" in header_type:
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


def log_sync(success: bool, message: str):
    db.collection("sync_logs").add({
        "job_name": "lottery_sync_github_actions",
        "run_type": "github_actions",
        "success": success,
        "message": message,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


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


def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")

        width, height = image.size
        scale = 2
        image = image.resize((width * scale, height * scale))

        text = pytesseract.image_to_string(
            image,
            config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789₹/-:., ",
        )

        return text
    except Exception as e:
        print(f"[WARN] OCR image extract failed: {e}")
        return ""


def clean_number_list(numbers):
    clean = []
    seen = set()

    for n in numbers:
        n = str(n).strip()
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


def parse_prize_numbers(raw_text: str, source: str):
    text = raw_text or ""
    parsed = empty_prize_numbers()
    parsed["parsed_source"] = source

    text_upper = text.upper()

    series_match = re.search(
        r"\b([0-9]{2}[A-Z])\s*[- ]?\s*([0-9]{5})\b",
        text_upper,
    )

    if series_match:
        parsed["first_prize_series"] = series_match.group(1)
        parsed["first_prize_number"] = series_match.group(2)
        parsed["consolation_number"] = series_match.group(2)

    five_digit_numbers = clean_number_list(re.findall(r"\b\d{5}\b", text))

    if parsed["first_prize_number"]:
        five_digit_numbers = [
            n for n in five_digit_numbers
            if n != parsed["first_prize_number"]
        ]

    parsed["second_prize"] = five_digit_numbers[:10]

    four_digit_numbers = re.findall(r"\b\d{4}\b", text)
    four_digit_numbers = [
        n for n in four_digit_numbers
        if not n.startswith("202") and n not in ("2025", "2026")
    ]
    four_digit_numbers = clean_number_list(four_digit_numbers)

    if len(four_digit_numbers) >= 120:
        parsed["fifth_prize"] = four_digit_numbers[:100]
        parsed["third_prize"] = four_digit_numbers[100:110]
        parsed["fourth_prize"] = four_digit_numbers[110:120]
    elif len(four_digit_numbers) >= 20:
        parsed["third_prize"] = four_digit_numbers[:10]
        parsed["fourth_prize"] = four_digit_numbers[10:20]
        parsed["fifth_prize"] = four_digit_numbers[20:]
    else:
        parsed["fifth_prize"] = []

    parsed["match_ready"] = bool(
        parsed["first_prize_number"]
        and (
            parsed["second_prize"]
            or parsed["third_prize"]
            or parsed["fourth_prize"]
            or parsed["fifth_prize"]
        )
    )

    return parsed


def save_result_doc(
    date_str,
    draw_label,
    draw_code,
    poster_storage_path,
    poster_url,
    poster_type,
    pdf_storage_path,
    pdf_url,
    source_page,
    parsed_numbers=None,
):
    doc_id = f"{date_str}_{draw_code}"
    doc_ref = db.collection("results").document(doc_id)
    existing = doc_ref.get()

    already_exists = existing.exists
    old_data = existing.to_dict() or {}

    created_at_value = old_data.get("created_at", firestore.SERVER_TIMESTAMP)
    notification_sent = old_data.get("notification_sent", False)

    data = {
        "date": date_str,
        "time": draw_label,
        "draw_code": draw_code,
        "result_type": "poster" if poster_url else "pdf",
        "poster_storage_path": poster_storage_path or "",
        "poster_url": poster_url or "",
        "poster_type": poster_type or "",
        "pdf_storage_path": pdf_storage_path or "",
        "pdf_url": pdf_url or "",
        "download_url": poster_url or pdf_url or "",
        "source_page": source_page,
        "status": "available",
        "updated_at": firestore.SERVER_TIMESTAMP,
        "created_at": created_at_value,
    }

    if parsed_numbers:
        data.update(parsed_numbers)

    doc_ref.set(data, merge=True)

    return {
        "is_new_doc": not already_exists,
        "notification_sent": notification_sent,
        "doc_ref": doc_ref,
    }


def mark_notification_sent(doc_ref):
    doc_ref.set({
        "notification_sent": True,
        "notification_sent_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


def looks_like_matching_draw_page(page_url: str, draw_label: str) -> bool:
    page_url = page_url.lower()
    if draw_label == "1 PM":
        return "1-pm" in page_url
    if draw_label == "6 PM":
        return "6-pm" in page_url
    if draw_label == "8 PM":
        return "8-pm" in page_url
    return False


def is_bad_placeholder(text: str) -> bool:
    bad_words = [
        "coming soon",
        "placeholder",
        "default",
        "no result",
        "not published",
    ]
    text = normalize_text(text)
    return any(word in text for word in bad_words)


def extract_best_poster_and_pdf(page_html: str, page_url: str, draw_label: str):
    if not looks_like_matching_draw_page(page_url, draw_label):
        return None, None

    soup = BeautifulSoup(page_html, "html.parser")
    page_text = normalize_text(soup.get_text(" ", strip=True))

    poster_url = None
    pdf_url = None

    for img in soup.find_all("img", src=True):
        src = urljoin(page_url, img.get("src", "").strip())
        alt = img.get("alt", "")
        parent_text = img.parent.get_text(" ", strip=True) if img.parent else ""
        combined = f"{src} {alt} {parent_text}"

        if is_bad_placeholder(combined):
            continue

        combined_norm = normalize_text(combined)

        if (
            "winner" in combined_norm
            or "result" in combined_norm
            or "lottery" in combined_norm
            or "dear" in combined_norm
        ):
            poster_url = src
            break

    pdf_candidates = []

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a.get("href", "").strip())
        text = a.get_text(" ", strip=True)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""
        combined = f"{href} {text} {parent_text}"

        if is_bad_placeholder(combined):
            continue

        combined_norm = normalize_text(combined)

        if (
            ".pdf" in href.lower()
            or "pdf" in combined_norm
            or "download" in combined_norm
            or "result download" in combined_norm
        ):
            pdf_candidates.append(href)

    for candidate in pdf_candidates:
        try:
            content, ext, _ = download_file(candidate)
            if ext == "pdf" and content[:4] == b"%PDF":
                pdf_url = candidate
                break
        except Exception as e:
            print(f"[WARN] PDF candidate failed -> {candidate} | {e}")

    if is_bad_placeholder(page_text) and not pdf_url:
        return None, None

    return poster_url, pdf_url


def sync_for_today():
    date_str = today_date()
    synced = 0
    only_draw_code = os.environ.get("ONLY_DRAW_CODE", "").strip().upper()

    for draw_label, page_url in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        if only_draw_code and draw_code != only_draw_code:
            continue

        try:
            page_html = fetch_page_with_retry(page_url)

            poster_source_url, pdf_source_url = extract_best_poster_and_pdf(
                page_html=page_html,
                page_url=page_url,
                draw_label=draw_label,
            )

            poster_storage_path = None
            poster_public_url = None
            poster_type = None
            pdf_storage_path = None
            pdf_public_url = None
            pdf_text = ""
            ocr_text = ""
            parsed_numbers = empty_prize_numbers()
            poster_content = None

            if poster_source_url:
                content, ext, content_type = download_file(poster_source_url)
                if ext in ("jpg", "png", "webp"):
                    poster_content = content
                    poster_storage_path, poster_public_url = upload_to_storage(
                        date_str=date_str,
                        draw_code=draw_code,
                        content=content,
                        ext=ext,
                        content_type=content_type,
                        kind="poster",
                    )
                    poster_type = ext

            if pdf_source_url:
                content, ext, content_type = download_file(pdf_source_url)
                if ext == "pdf":
                    pdf_text = extract_text_from_pdf_bytes(content)
                    pdf_storage_path, pdf_public_url = upload_to_storage(
                        date_str=date_str,
                        draw_code=draw_code,
                        content=content,
                        ext=ext,
                        content_type=content_type,
                        kind="pdf",
                    )

            if pdf_text.strip():
                parsed_numbers = parse_prize_numbers(pdf_text, "pdf")
            elif poster_content:
                ocr_text = extract_text_from_image_bytes(poster_content)
                parsed_numbers = parse_prize_numbers(ocr_text, "poster_ocr")
            else:
                parsed_numbers = empty_prize_numbers()
                parsed_numbers["parsed_source"] = "no_pdf_no_poster"

            parsed_numbers["ocr_text_preview"] = ocr_text[:1000] if ocr_text else ""

            if not poster_public_url and not pdf_public_url:
                print(f"[MISS] {date_str} {draw_label} -> no valid {draw_label} result found")
                continue

            save_info = save_result_doc(
                date_str=date_str,
                draw_label=draw_label,
                draw_code=draw_code,
                poster_storage_path=poster_storage_path,
                poster_url=poster_public_url,
                poster_type=poster_type,
                pdf_storage_path=pdf_storage_path,
                pdf_url=pdf_public_url,
                source_page=page_url,
                parsed_numbers=parsed_numbers,
            )

            synced += 1

            print(
                f"[OK] {date_str} {draw_label} -> "
                f"poster={poster_public_url or 'none'} | "
                f"pdf={pdf_public_url or 'none'} | "
                f"parsed_source={parsed_numbers.get('parsed_source')} | "
                f"match_ready={parsed_numbers.get('match_ready')}"
            )

            if (
                parsed_numbers.get("match_ready") is True
                and not save_info["notification_sent"]
            ):
                send_result_notification(date_str, draw_label)
                mark_notification_sent(save_info["doc_ref"])
            else:
                print(
                    f"[INFO] Notification skipped for {draw_label} | "
                    f"match_ready={parsed_numbers.get('match_ready')} | "
                    f"already_sent={save_info['notification_sent']}"
                )

        except Exception as e:
            print(f"[ERROR] {date_str} {draw_label} -> {e}")

    log_sync(True, f"{date_str}: synced {synced} result(s)")


if __name__ == "__main__":
    try:
        sync_for_today()
    except Exception as e:
        msg = f"{today_date()} failed: {e}"
        print(f"[FAIL] {msg}")
        log_sync(False, msg)
