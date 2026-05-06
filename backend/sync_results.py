import os
import re
import json
from io import BytesIO
from datetime import datetime
from urllib.parse import urljoin

import fitz
import requests
import pytesseract
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from bs4 import BeautifulSoup

import firebase_admin
from firebase_admin import credentials, firestore, storage, messaging


BASE_URL = "https://lotterysambadresult.in/"
BUCKET_NAME = "wb-lottery-result-live.firebasestorage.app"

DRAW_PAGES = {
    "1 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
    "6 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
    "8 PM": "https://lotterysambadresult.in/lottery-sambad-today-result-08-00-pm.html",
}

DRAW_CODES = {
    "1 PM": "1PM",
    "6 PM": "6PM",
    "8 PM": "8PM",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": BASE_URL,
}


def init_firebase():
    firebase_key = os.environ.get("FIREBASE_KEY")

    if not firebase_key:
        raise RuntimeError("FIREBASE_KEY environment variable missing")

    cred = credentials.Certificate(json.loads(firebase_key))

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

    return firestore.client(), storage.bucket()


db, bucket = init_firebase()


def today_date():
    return datetime.now().strftime("%Y-%m-%d")


def get_allowed_draws():
    now = datetime.now()
    hour = now.hour
    minute = now.minute

    force_draw = os.environ.get("FORCE_DRAW", "").strip().upper()

    if force_draw in ["1PM", "6PM", "8PM"]:
        return [force_draw]

    if hour == 13 and minute in [15, 20, 25]:
        return ["1PM"]

    if hour == 18 and minute in [15, 20, 25]:
        return ["6PM"]

    if hour == 20 and minute in [15, 20, 25]:
        return ["8PM"]

    return []


def is_bad_url(url: str) -> bool:
    url = (url or "").lower()

    bad_words = [
        "logo",
        "banner",
        "youtube",
        "telegram",
        "whatsapp",
        "facebook",
        "ads",
        "advertisement",
        "cs101",
        "ed14",
        "lottery-sambad.png",
        "install",
        "app",
        "playstore",
        "icon",
        "favicon",
    ]

    return any(word in url for word in bad_words)


def detect_file_type(content: bytes):
    if content[:4] == b"%PDF":
        return "pdf", "application/pdf"

    if content[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"

    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"

    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"

    return None, None


def download(url: str):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()

    content = response.content
    ext, content_type = detect_file_type(content)

    return content, ext, content_type


def upload_to_storage(path: str, content: bytes, content_type: str):
    blob = bucket.blob(path)
    blob.upload_from_string(content, content_type=content_type)
    blob.make_public()

    print(f"[STORAGE OK] {path}")

    return blob.public_url


def fetch_html(url: str):
    response = requests.get(url, headers=HEADERS, timeout=35)
    response.raise_for_status()
    return response.text


def unique_list(items):
    clean = []
    seen = set()

    for item in items:
        if not item:
            continue

        item = item.strip()

        if item not in seen:
            clean.append(item)
            seen.add(item)

    return clean


def extract_sources(html: str, page_url: str):
    pdfs = []
    posters = []

    soup = BeautifulSoup(html, "html.parser")

    pdfs.extend(
        re.findall(
            r'https?://[^"\']+?\.pdf(?:\?[^"\']*)?',
            html,
            flags=re.I,
        )
    )

    posters.extend(
        re.findall(
            r'https?://[^"\']+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?',
            html,
            flags=re.I,
        )
    )

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a.get("href", "").strip())
        href_lower = href.lower()
        text = a.get_text(" ", strip=True).lower()

        if ".pdf" in href_lower:
            pdfs.append(href)

        if "download" in text and ".pdf" in href_lower:
            pdfs.append(href)

        if any(ext in href_lower for ext in [".jpg", ".jpeg", ".png", ".webp"]):
            posters.append(href)

    for img in soup.find_all("img"):
        for attr in ["src", "data-src", "data-lazy-src", "data-original"]:
            src = img.get(attr)

            if src:
                posters.append(urljoin(page_url, src.strip()))

        srcset = img.get("srcset") or img.get("data-srcset")

        if srcset:
            for part in srcset.split(","):
                src = part.strip().split(" ")[0].strip()

                if src:
                    posters.append(urljoin(page_url, src))

    pdfs = unique_list([p for p in pdfs if ".pdf" in p.lower()])

    posters = unique_list([
        p for p in posters
        if any(ext in p.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
        and not is_bad_url(p)
    ])

    pdfs = sorted(
        pdfs,
        key=lambda x: (
            0 if "/wp-content/uploads/" in x.lower() else 1,
            0 if "2026" in x.lower() else 1,
            0 if "pdf_" in x.lower() else 1,
        ),
    )

    posters = sorted(
        posters,
        key=lambda x: (
            0 if "/wp-content/uploads/" in x.lower() else 1,
            0 if "2026" in x.lower() else 1,
            0 if "img_" in x.lower() else 1,
        ),
    )

    print(f"[CANDIDATES] pdf={len(pdfs)}, poster={len(posters)}")
    print("[PDF]", pdfs[:5])
    print("[POSTER]", posters[:5])

    return pdfs, posters


def empty_parsed_data():
    return {
        "result_date": "",
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
        "parse_confidence": 0,
        "needs_review": True,
        "parsed_source": "none",
    }


def detect_result_date(text: str):
    matches = re.findall(r"\b(\d{2})[/-](\d{2})[/-](\d{2,4})\b", text)

    for dd, mm, yy in matches:
        if yy == "00":
            continue

        year = int(yy)

        if year < 100:
            year += 2000

        try:
            return datetime(year, int(mm), int(dd)).strftime("%Y-%m-%d")
        except Exception:
            continue

    return ""


def clean_unique_numbers(numbers, length):
    clean = []
    seen = set()

    for num in numbers:
        num = str(num).strip()

        if not re.fullmatch(rf"\d{{{length}}}", num):
            continue

        if length == 4 and num in {"2026", "3933"}:
            continue

        if num not in seen:
            clean.append(num)
            seen.add(num)

    return clean


def find_first_prize(text: str):
    patterns = [
        r"\b([0-9]{2}[A-Z])\s+([0-9]{5})\b",
        r"\b([0-9]{2})\s*([A-Z])\s+([0-9]{5})\b",
        r"\b([0-9]{2}[A-Z])\s*[-:]*\s*([0-9]{5})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            groups = match.groups()

            if len(groups) == 2:
                return groups[0], groups[1]

            return f"{groups[0]}{groups[1]}", groups[2]

    return "", ""


def extract_between(text: str, start_patterns, end_patterns):
    start_pos = -1

    for pattern in start_patterns:
        match = re.search(pattern, text)

        if match:
            start_pos = match.end()
            break

    if start_pos == -1:
        return ""

    end_pos = len(text)

    for pattern in end_patterns:
        match = re.search(pattern, text[start_pos:])

        if match:
            end_pos = start_pos + match.start()
            break

    return text[start_pos:end_pos]


def build_parsed_data_from_text(text: str, source_name: str):
    parsed = empty_parsed_data()

    text = (text or "").upper()

    parsed["result_date"] = detect_result_date(text)

    first_series, first_number = find_first_prize(text)

    parsed["first_prize_series"] = first_series
    parsed["first_prize_number"] = first_number
    parsed["consolation_number"] = first_number
    parsed["parsed_source"] = source_name

    second_block = extract_between(
        text,
        [r"2ND", r"2\s*ND", r"SECOND"],
        [r"3RD", r"3\s*RD", r"THIRD"],
    )

    third_block = extract_between(
        text,
        [r"3RD", r"3\s*RD", r"THIRD"],
        [r"4TH", r"4\s*TH", r"FOURTH"],
    )

    fourth_block = extract_between(
        text,
        [r"4TH", r"4\s*TH", r"FOURTH"],
        [r"5TH", r"5\s*TH", r"FIFTH"],
    )

    fifth_block = extract_between(
        text,
        [r"5TH", r"5\s*TH", r"FIFTH"],
        [r"TDS", r"DRAW", r"SHALL"],
    )

    second_numbers = clean_unique_numbers(re.findall(r"\b\d{5}\b", second_block), 5)
    third_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", third_block), 4)
    fourth_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", fourth_block), 4)
    fifth_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", fifth_block), 4)

    all_5 = clean_unique_numbers(re.findall(r"\b\d{5}\b", text), 5)
    all_4 = clean_unique_numbers(re.findall(r"\b\d{4}\b", text), 4)

    if first_number and first_number in all_5:
        all_5.remove(first_number)

    if len(second_numbers) < 10:
        second_numbers = all_5[:10]

    if len(third_numbers) < 10:
        third_numbers = all_4[:10]

    if len(fourth_numbers) < 10:
        fourth_numbers = all_4[10:20]

    if len(fifth_numbers) < 50:
        fifth_numbers = all_4[20:120]

    parsed["second_prize"] = second_numbers[:10]
    parsed["third_prize"] = third_numbers[:10]
    parsed["fourth_prize"] = fourth_numbers[:10]
    parsed["fifth_prize"] = fifth_numbers[:120]

    confidence = 0

    if parsed["result_date"]:
        confidence += 10

    if parsed["first_prize_series"] and parsed["first_prize_number"]:
        confidence += 30

    if len(parsed["second_prize"]) >= 10:
        confidence += 20

    if len(parsed["third_prize"]) >= 10:
        confidence += 15

    if len(parsed["fourth_prize"]) >= 10:
        confidence += 15

    if len(parsed["fifth_prize"]) >= 50:
        confidence += 10

    parsed["parse_confidence"] = confidence
    parsed["needs_review"] = confidence < 70

    print("[PARSE OK]")
    print(json.dumps(parsed, ensure_ascii=False, indent=2))

    return parsed


def extract_pdf_text(content: bytes) -> str:
    doc = fitz.open(stream=content, filetype="pdf")
    full_text = ""

    for page in doc:
        full_text += "\n" + page.get_text("text")

    return full_text.upper()


def extract_lottery_data_from_pdf(content: bytes):
    try:
        text = extract_pdf_text(content)

        print("\n========== PDF TEXT PREVIEW ==========")
        print(text[:5000])
        print("========== PDF TEXT END ==========\n")

        return build_parsed_data_from_text(text, "pdf_text")

    except Exception as e:
        print(f"[PDF PARSE ERROR] {e}")
        return empty_parsed_data()


def preprocess_image_for_ocr(content: bytes):
    image = Image.open(BytesIO(content)).convert("RGB")

    width, height = image.size

    if width < 1600:
        scale = 1600 / width
        image = image.resize(
            (int(width * scale), int(height * scale)),
            Image.LANCZOS,
        )

    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(2.2)
    image = ImageEnhance.Sharpness(image).enhance(2.0)
    image = image.filter(ImageFilter.SHARPEN)

    return image


def extract_lottery_data_from_image(content: bytes):
    try:
        image = preprocess_image_for_ocr(content)

        text = pytesseract.image_to_string(
            image,
            config="--oem 3 --psm 6",
        )

        print("\n========== POSTER OCR TEXT PREVIEW ==========")
        print(text[:5000])
        print("========== POSTER OCR TEXT END ==========\n")

        text = text.upper()
        text = text.replace("O", "0")
        text = text.replace("I", "1")
        text = text.replace("S", "5")

        return build_parsed_data_from_text(text, "poster_ocr")

    except Exception as e:
        print(f"[POSTER OCR ERROR] {e}")
        return empty_parsed_data()


def result_already_notified(date_str, draw_code):
    doc_id = f"{date_str}_{draw_code}"

    try:
        doc = db.collection("Results").document(doc_id).get()

        if doc.exists:
            return bool((doc.to_dict() or {}).get("notification_sent", False))

    except Exception:
        pass

    return False


def mark_notification_sent(date_str, draw_code):
    doc_id = f"{date_str}_{draw_code}"

    data = {
        "notification_sent": True,
        "notification_sent_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection("Results").document(doc_id).set(data, merge=True)


def save_result_doc(
    date_str,
    draw_label,
    draw_code,
    pdf_url,
    poster_url,
    source_page,
    parsed_data,
    notification_sent,
):
    doc_id = f"{date_str}_{draw_code}"

    data = {
        "date": date_str,
        "result_date": parsed_data.get("result_date", ""),
        "time": draw_label,
        "draw_code": draw_code,
        "pdf_url": pdf_url or "",
        "poster_url": poster_url or "",
        "download_url": pdf_url or poster_url or "",
        "source_page": source_page,
        "status": "available",
        "match_ready": True,
        "first_prize_series": parsed_data.get("first_prize_series", ""),
        "first_prize_number": parsed_data.get("first_prize_number", ""),
        "consolation_number": parsed_data.get("consolation_number", ""),
        "second_prize": parsed_data.get("second_prize", []),
        "third_prize": parsed_data.get("third_prize", []),
        "fourth_prize": parsed_data.get("fourth_prize", []),
        "fifth_prize": parsed_data.get("fifth_prize", []),
        "prize_amounts": parsed_data.get("prize_amounts", {}),
        "parse_confidence": parsed_data.get("parse_confidence", 0),
        "needs_review": parsed_data.get("needs_review", True),
        "parsed_source": parsed_data.get("parsed_source", "none"),
        "notification_sent": notification_sent,
        "updated_at": firestore.SERVER_TIMESTAMP,
        "created_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection("Results").document(doc_id).set(data, merge=True)

    print(f"[FIRESTORE OK] Results/{doc_id}")


def send_notification(date_str, draw_label, draw_code):
    try:
        message = messaging.Message(
            topic="all_users",
            notification=messaging.Notification(
                title="WB Lottery Result – LIVE",
                body=f"{draw_label} Result Published ({date_str})",
            ),
            data={
                "type": "result_published",
                "date": date_str,
                "time": draw_label,
                "draw_code": draw_code,
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="default_channel",
                    sound="default",
                ),
            ),
        )

        response = messaging.send(message)

        print(f"[TOPIC NOTIFICATION SENT] {response}")

    except Exception as e:
        print(f"[NOTIFICATION ERROR] {e}")
        raise


def process_draw(date_str, draw_label, page_url):
    draw_code = DRAW_CODES[draw_label]

    print(f"\n==== {draw_label} ====")
    print(f"[FETCH] {page_url}")

    html = fetch_html(page_url)
    pdfs, posters = extract_sources(html, page_url)

    pdf_public_url = ""
    poster_public_url = ""
    parsed_data = empty_parsed_data()

    for pdf in pdfs:
        try:
            content, ext, content_type = download(pdf)

            print(f"[CHECK PDF] {pdf} | ext={ext} | size={len(content)}")

            if ext == "pdf":
                parsed_data = extract_lottery_data_from_pdf(content)

                detected_date = parsed_data.get("result_date", "")

                if detected_date and detected_date != date_str:
                    print(f"[SKIP OLD PDF] detected={detected_date}, today={date_str}")
                    continue

                path = f"results/{date_str}/{draw_code}_pdf.pdf"
                pdf_public_url = upload_to_storage(path, content, content_type)
                break

        except Exception as e:
            print(f"[PDF SKIP] {pdf} | {e}")

    for poster in posters:
        try:
            content, ext, content_type = download(poster)

            print(f"[CHECK POSTER] {poster} | ext={ext} | size={len(content)}")

            if ext in ["jpg", "png", "webp"] and len(content) > 5000:
                path = f"results/{date_str}/{draw_code}_poster.{ext}"
                poster_public_url = upload_to_storage(path, content, content_type)

                if not pdf_public_url:
                    parsed_data = extract_lottery_data_from_image(content)

                break

        except Exception as e:
            print(f"[POSTER SKIP] {poster} | {e}")

    if not pdf_public_url and not poster_public_url:
        print(f"[MISS] No valid PDF/poster for {draw_label}")
        return False

    if parsed_data.get("parse_confidence", 0) < 50:
        print("[WARN] Low parse confidence, saving with needs_review=true")

    already_notified = result_already_notified(date_str, draw_code)

    save_result_doc(
        date_str=date_str,
        draw_label=draw_label,
        draw_code=draw_code,
        pdf_url=pdf_public_url,
        poster_url=poster_public_url,
        source_page=page_url,
        parsed_data=parsed_data,
        notification_sent=already_notified,
    )

    if already_notified:
        print(f"[NOTIFICATION SKIP] already sent for {date_str}_{draw_code}")
    else:
        send_notification(date_str, draw_label, draw_code)
        mark_notification_sent(date_str, draw_code)

    return True


def run():
    print("SYNC FILE STARTED")
    print("RUN FUNCTION STARTED")

    date_str = today_date()
    success = 0
    allowed_draws = get_allowed_draws()

    print(f"[DATE] {date_str}")
    print(f"[BUCKET] {BUCKET_NAME}")
    print(f"[ALLOWED DRAWS] {allowed_draws}")

    if not allowed_draws:
        print("[SKIP] Current time is not result sync time")
        return

    for draw_label, page_url in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        if draw_code not in allowed_draws:
            print(f"[SKIP DRAW] {draw_label}")
            continue

        try:
            ok = process_draw(date_str, draw_label, page_url)

            if ok:
                success += 1

        except Exception as e:
            import traceback

            print(f"[ERROR] {draw_label}: {e}")
            traceback.print_exc()

    print(f"\n[DONE] synced={success}")


if __name__ == "__main__":
    run()
