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
    print(f"[STORAGE OK] {storage_path}")
    return storage_path, blob.public_url


def convert_pdf_to_poster_webp(pdf_bytes: bytes):
    try:
        images = convert_from_bytes(pdf_bytes, first_page=1, last_page=1, dpi=300)

        if not images:
            print("[WARN] PDF convert returned no image")
            return None

        buffer = BytesIO()
        images[0].convert("RGB").save(buffer, format="WEBP", quality=95)
        poster_bytes = buffer.getvalue()

        if len(poster_bytes) < 30000:
            print("[WARN] Generated poster too small, skipping")
            return None

        return poster_bytes
    except Exception as e:
        print(f"[WARN] PDF to poster conversion failed: {e}")
        return None


def log_sync(success: bool, message: str):
    try:
        db.collection("sync_logs").add({
            "job_name": "lottery_sync_github_actions",
            "run_type": "github_actions",
            "success": success,
            "message": message,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[WARN] sync log save failed: {e}")


def already_final_synced(date_str: str, draw_code: str) -> bool:
    doc = db.collection("results").document(f"{date_str}_{draw_code}").get()

    if not doc.exists:
        return False

    data = doc.to_dict() or {}

    return bool(data.get("download_url")) and bool(data.get("notification_sent"))


def send_result_notification(date_str: str, draw_label: str):
    print(f"[NOTIFICATION] Checking tokens for {draw_label}")

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
        return 0

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
                        channel_id="win_channel",
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

    print(f"[NOTIFICATION OK] sent={success_count}, invalid={len(invalid_tokens)}")
    return success_count


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = []

        for page in reader.pages:
            texts.append(page.extract_text() or "")

        text = "\n".join(texts)
        print(f"[PDF TEXT] chars={len(text)}")
        return text
    except Exception as e:
        print(f"[WARN] PDF text extract failed: {e}")
        return ""


def prepare_image_for_ocr(image: Image.Image, threshold_value: int = 145) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    image = image.resize((width * 4, height * 4))
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image)
    image = image.filter(ImageFilter.SHARPEN)
    image = image.point(lambda x: 0 if x < threshold_value else 255)
    return image


def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    try:
        original_image = Image.open(BytesIO(image_bytes)).convert("RGB")
        ocr_outputs = []

        for threshold in [100, 115, 130, 145, 160, 175, 190]:
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

        try:
            fallback_text = pytesseract.image_to_string(
                original_image,
                config="--oem 3 --psm 11",
            )
            if fallback_text.strip():
                ocr_outputs.append(fallback_text)
        except Exception as e:
            print(f"[WARN] OCR fallback failed: {e}")

        final_text = "\n".join(ocr_outputs)

        print("[OCR PREVIEW]")
        print(final_text[:1000])

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
        "parse_confidence": 0,
        "needs_review": True,
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


def extract_result_date(text: str) -> str:
    text = text or ""

    patterns = [
        r"\b(\d{2})/(\d{2})/(\d{2})\b",
        r"\b(\d{2})-(\d{2})-(\d{2})\b",
        r"\b(\d{2})/(\d{2})/(\d{4})\b",
        r"\b(\d{2})-(\d{2})-(\d{4})\b",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)

        for day, month, year in matches:
            try:
                yyyy = year if len(year) == 4 else f"20{year}"
                parsed = datetime.strptime(f"{yyyy}-{month}-{day}", "%Y-%m-%d")
                return parsed.strftime("%Y-%m-%d")
            except Exception:
                continue

    return ""


def extract_first_prize(text: str):
    text_upper = (text or "").upper()

    patterns = [
        r"\b([0-9]{2}[A-Z])\s*[- ]?\s*([0-9]{5})\b",
        r"\b([0-9]{2})\s*([A-Z])\s*[- ]?\s*([0-9]{5})\b",
        r"\b([0-9]{2}[A-Z])\s*([0-9]\s*[0-9]\s*[0-9]\s*[0-9]\s*[0-9])\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text_upper)

        if not match:
            continue

        groups = match.groups()

        if len(groups) == 2:
            series = groups[0].replace(" ", "")
            number = re.sub(r"\s+", "", groups[1])
        else:
            series = groups[0] + groups[1]
            number = re.sub(r"\s+", "", groups[2])

        if re.match(r"^\d{2}[A-Z]$", series) and re.match(r"^\d{5}$", number):
            return series, number

    return "", ""


def split_text_by_prize_blocks(text: str):
    upper = (text or "").upper()

    block_patterns = {
        "second": r"(2ND|SECOND|2 ND)",
        "third": r"(3RD|THIRD|3 RD)",
        "fourth": r"(4TH|FOURTH|4 TH)",
        "fifth": r"(5TH|FIFTH|5 TH)",
    }

    positions = []

    for key, pattern in block_patterns.items():
        match = re.search(pattern, upper)
        if match:
            positions.append((match.start(), key))

    positions.sort()

    blocks = {
        "second": "",
        "third": "",
        "fourth": "",
        "fifth": "",
    }

    if not positions:
        return blocks

    for index, (start, key) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        blocks[key] = text[start:end]

    return blocks


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
        parsed["match_ready"] = False
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
        fifth_numbers = clean_number_list(fifth_numbers + all_four[20:])[:60]

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
    if len(parsed["fifth_prize"]) >= 20:
        confidence += 20

    parsed["parse_confidence"] = confidence
    parsed["needs_review"] = confidence < 65

    parsed["match_ready"] = bool(
        parsed["first_prize_number"]
        or parsed["second_prize"]
        or parsed["third_prize"]
        or parsed["fourth_prize"]
        or parsed["fifth_prize"]
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

    doc_ref_lower = db.collection("results").document(doc_id)
    doc_ref_upper = db.collection("Results").document(doc_id)

    existing = doc_ref_lower.get()
    old_data = existing.to_dict() or {}
    notification_sent = old_data.get("notification_sent", False)

    data = {
        "date": date_str,
        "time": draw_label,
        "draw_code": draw_code,
        "result_type": "pdf" if pdf_url else "poster",
        "poster_storage_path": poster_storage_path or old_data.get("poster_storage_path", ""),
        "poster_url": poster_url or old_data.get("poster_url", ""),
        "poster_type": poster_type or old_data.get("poster_type", ""),
        "pdf_storage_path": pdf_storage_path or old_data.get("pdf_storage_path", ""),
        "pdf_url": pdf_url or old_data.get("pdf_url", ""),
        "download_url": pdf_url or old_data.get("pdf_url", "") or poster_url or old_data.get("poster_url", ""),
        "source_page": source_page,
        "status": "available",
        "updated_at": firestore.SERVER_TIMESTAMP,
        "created_at": old_data.get("created_at", firestore.SERVER_TIMESTAMP),
    }

    if parsed_numbers:
        data.update(parsed_numbers)

    doc_ref_lower.set(data, merge=True)
    doc_ref_upper.set(data, merge=True)

    print(f"[FIRESTORE OK] results/{doc_id}")
    print(f"[FIRESTORE OK] Results/{doc_id}")

    return {
        "is_new_doc": not existing.exists,
        "notification_sent": notification_sent,
        "doc_ref": doc_ref_lower,
    }


def mark_notification_sent(doc_ref, sent_count: int):
    doc_ref.set({
        "notification_sent": True,
        "notification_sent_count": sent_count,
        "notification_sent_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


def is_bad_placeholder(text: str) -> bool:
    bad_words = [
        "coming soon",
        "placeholder",
        "default",
        "no result",
        "not published",
        "google play",
        "get it on",
        "youtube",
        "app store",
        "logo",
        "banner",
        "advertisement",
        "ads",
    ]

    text = normalize_text(text)
    return any(word in text for word in bad_words)


def extract_best_pdf_and_poster(page_html: str, page_url: str):
    soup = BeautifulSoup(page_html, "html.parser")

    pdf_candidates = []
    poster_candidates = []

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

    for img in soup.find_all("img", src=True):
        src = urljoin(page_url, img.get("src", "").strip())
        alt = img.get("alt", "")
        parent_text = img.parent.get_text(" ", strip=True) if img.parent else ""
        combined = f"{src} {alt} {parent_text}"

        if is_bad_placeholder(combined):
            continue

        combined_norm = normalize_text(combined)

        if any(x in src.lower() for x in [".jpg", ".jpeg", ".png", ".webp"]) and (
            "result" in combined_norm
            or "dear" in combined_norm
            or "lottery" in combined_norm
            or "sambad" in combined_norm
            or "nagaland" in combined_norm
        ):
            poster_candidates.append(src)

    print(f"[CANDIDATES] pdf={len(pdf_candidates)}, poster={len(poster_candidates)}")

    pdf_url = None
    poster_url = None

    for candidate in pdf_candidates:
        try:
            content, ext, _ = download_file(candidate)

            if ext == "pdf" and content[:4] == b"%PDF":
                print(f"[INFO] Valid PDF found: {candidate}")
                pdf_url = candidate
                break

        except Exception as e:
            print(f"[WARN] PDF candidate failed -> {candidate} | {e}")

    for candidate in poster_candidates:
        try:
            content, ext, _ = download_file(candidate)

            if ext in ("jpg", "png", "webp") and len(content) >= 10000:
                print(f"[INFO] Valid poster found: {candidate} size={len(content)}")
                poster_url = candidate
                break

            print(f"[SKIP] Invalid/small poster -> {candidate} size={len(content)}")

        except Exception as e:
            print(f"[WARN] Poster candidate failed -> {candidate} | {e}")

    return pdf_url, poster_url


def validate_result_date(date_str: str, text: str, source: str) -> bool:
    detected_date = extract_result_date(text)

    if not detected_date:
        print(f"[WARN] Date not detected from {source}. Continue carefully.")
        return True

    if detected_date != date_str:
        print(f"[SKIP OLD RESULT] source={source} detected={detected_date}, today={date_str}")
        return False

    return True


def process_single_draw(date_str: str, draw_label: str, page_url: str):
    draw_code = DRAW_CODES[draw_label]

    if already_final_synced(date_str, draw_code):
        print(f"[SKIP] {date_str} {draw_label} already synced and notified")
        return True

    page_html = fetch_page_with_retry(page_url)

    pdf_source_url, poster_source_url = extract_best_pdf_and_poster(
        page_html=page_html,
        page_url=page_url,
    )

    if not pdf_source_url and not poster_source_url:
        print(f"[ERROR] No result source found for {draw_label}")
        return False

    poster_storage_path = None
    poster_public_url = None
    poster_type = None
    pdf_storage_path = None
    pdf_public_url = None
    parsed_numbers = empty_prize_numbers()
    ocr_text = ""

    if pdf_source_url:
        content, ext, _ = download_file(pdf_source_url)

        if ext != "pdf":
            print(f"[MISS] {date_str} {draw_label} -> downloaded file is not PDF")
            return False

        pdf_text = extract_text_from_pdf_bytes(content)

        if not validate_result_date(date_str, pdf_text, "pdf"):
            return False

        pdf_storage_path, pdf_public_url = upload_to_storage(
            date_str=date_str,
            draw_code=draw_code,
            content=content,
            ext="pdf",
            content_type="application/pdf",
            kind="pdf",
        )

        poster_bytes = convert_pdf_to_poster_webp(content)

        if poster_bytes:
            poster_storage_path, poster_public_url = upload_to_storage(
                date_str=date_str,
                draw_code=draw_code,
                content=poster_bytes,
                ext="webp",
                content_type="image/webp",
                kind="poster",
            )
            poster_type = "webp"

        if pdf_text.strip():
            parsed_numbers = parse_prize_numbers(pdf_text, "pdf")
        elif poster_bytes:
            ocr_text = extract_text_from_image_bytes(poster_bytes)

            if not validate_result_date(date_str, ocr_text, "pdf_poster_ocr"):
                return False

            parsed_numbers = parse_prize_numbers(ocr_text, "pdf_poster_ocr")
        else:
            parsed_numbers["parsed_source"] = "pdf_no_text"

    elif poster_source_url:
        content, ext, content_type = download_file(poster_source_url)

        if ext not in ("jpg", "png", "webp") or len(content) < 10000:
            print(f"[MISS] {date_str} {draw_label} -> valid poster not found")
            return False

        ocr_text = extract_text_from_image_bytes(content)

        if not validate_result_date(date_str, ocr_text, "poster_ocr"):
            return False

        poster_storage_path, poster_public_url = upload_to_storage(
            date_str=date_str,
            draw_code=draw_code,
            content=content,
            ext=ext,
            content_type=content_type,
            kind="poster",
        )
        poster_type = ext

        parsed_numbers = parse_prize_numbers(ocr_text, "poster_ocr")

    parsed_numbers["ocr_text_preview"] = ocr_text[:1200] if ocr_text else ""

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

    print(
        f"[OK] {date_str} {draw_label} -> "
        f"poster={poster_public_url or 'none'} | "
        f"pdf={pdf_public_url or 'none'} | "
        f"parsed_source={parsed_numbers.get('parsed_source')} | "
        f"confidence={parsed_numbers.get('parse_confidence')} | "
        f"needs_review={parsed_numbers.get('needs_review')} | "
        f"match_ready={parsed_numbers.get('match_ready')}"
    )

    print("[DEBUG DATA]")
    print(json.dumps({
        "first_prize_series": parsed_numbers.get("first_prize_series"),
        "first_prize_number": parsed_numbers.get("first_prize_number"),
        "second_prize_count": len(parsed_numbers.get("second_prize", [])),
        "third_prize_count": len(parsed_numbers.get("third_prize", [])),
        "fourth_prize_count": len(parsed_numbers.get("fourth_prize", [])),
        "fifth_prize_count": len(parsed_numbers.get("fifth_prize", [])),
        "match_ready": parsed_numbers.get("match_ready"),
        "parse_confidence": parsed_numbers.get("parse_confidence"),
    }, ensure_ascii=False, indent=2))

    if not save_info["notification_sent"]:
        sent_count = send_result_notification(date_str, draw_label)
        mark_notification_sent(save_info["doc_ref"], sent_count)
    else:
        print("[INFO] Notification skipped because already sent")

    return True


def sync_for_today():
    date_str = today_date()
    synced = 0
    only_draw_code = os.environ.get("ONLY_DRAW_CODE", "").strip().upper()

    print(f"[START] Auto Result Sync date={date_str}, only_draw_code={only_draw_code or 'ALL'}")

    for draw_label, page_url in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        if only_draw_code and draw_code != only_draw_code:
            print(f"[SKIP] ONLY_DRAW_CODE={only_draw_code}, skipping {draw_code}")
            continue

        found_result = False

        for attempt in range(1, 7):
            try:
                print(f"[TRY {attempt}/6] Checking {date_str} {draw_label}")

                found_result = process_single_draw(
                    date_str=date_str,
                    draw_label=draw_label,
                    page_url=page_url,
                )

                if found_result:
                    synced += 1
                    break

                if attempt < 6:
                    print(f"[WAIT] {date_str} {draw_label} not ready. Retry after 15 seconds...")
                    time.sleep(15)

            except Exception as e:
                print(f"[ERROR TRY {attempt}/6] {date_str} {draw_label} -> {e}")

                if attempt < 6:
                    time.sleep(15)
                else:
                    print(f"[FAILED] {date_str} {draw_label} after 6 tries")

        if not found_result:
            print(f"[FINAL MISS] {date_str} {draw_label}")

    log_sync(True, f"{date_str}: synced {synced} result(s)")
    print(f"[DONE] {date_str}: synced {synced} result(s)")


if __name__ == "__main__":
    try:
        sync_for_today()
    except Exception as e:
        msg = f"{today_date()} failed: {e}"
        print(f"[FAIL] {msg}")
        log_sync(False, msg)
        raise
