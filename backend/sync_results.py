import os
import re
import io
import json
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
    "1 PM": [
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
        "https://lotterysambadresult.in/lottery-sambad-today-result-01-00-pm.html",
        "https://lotterysambadresult.in/",
    ],
    "6 PM": [
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
        "https://lotterysambadresult.in/lottery-sambad-today-result-06-00-pm.html",
        "https://lotterysambadresult.in/lottery-sambad-today-result-6-00-pm.html",
        "https://lotterysambadresult.in/",
    ],
    "8 PM": [
        "https://lotterysambadresult.in/lottery-sambad-today-result-08-00-pm.html",
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-8-pm.html",
        "https://lotterysambadresult.in/",
    ],
}

DRAW_CODES = {
    "1 PM": "1PM",
    "6 PM": "6PM",
    "8 PM": "8PM",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": BASE_URL,
}

db = None
bucket = None


def init_firebase():
    firebase_key = os.environ.get("FIREBASE_KEY", "").strip()

    if not firebase_key:
        raise RuntimeError("FIREBASE_KEY is empty. Check GitHub Secret FIREBASE_SERVICE_ACCOUNT")

    cred = credentials.Certificate(json.loads(firebase_key))

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

    return firestore.client(), storage.bucket()


def today_date():
    return datetime.now().strftime("%Y-%m-%d")


def get_allowed_draws():
    force_draw = os.environ.get("FORCE_DRAW", "").strip().upper()

    if force_draw in ["1PM", "6PM", "8PM"]:
        return [force_draw]

    now = datetime.now()
    hour = now.hour
    minute = now.minute

    if hour == 13 and minute in [15, 20, 25]:
        return ["1PM"]
    if hour == 18 and minute in [15, 20, 25]:
        return ["6PM"]
    if hour == 20 and minute in [15, 20, 25]:
        return ["8PM"]

    return []


def is_bad_url(url):
    url = (url or "").lower()
    bad_words = [
        "logo", "banner", "youtube", "telegram", "whatsapp", "facebook",
        "ads", "advertisement", "cs101", "ed14", "lottery-sambad.png",
        "install", "app", "playstore", "icon", "favicon",
    ]
    return any(x in url for x in bad_words)


def detect_file_type(content):
    if content[:4] == b"%PDF":
        return "pdf", "application/pdf"
    if content[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None


def download(url):
    response = requests.get(url, headers=HEADERS, timeout=45)
    response.raise_for_status()
    content = response.content
    ext, content_type = detect_file_type(content)
    return content, ext, content_type


def upload_to_storage(path, content, content_type):
    blob = bucket.blob(path)
    blob.upload_from_string(content, content_type=content_type)
    blob.make_public()
    print(f"[STORAGE OK] {path}", flush=True)
    return blob.public_url


def fetch_html(url):
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


def draw_keywords(draw_label):
    if draw_label == "1 PM":
        return ["1pm", "1-pm", "01-00", "1-00", "01pm", "1 pm"]
    if draw_label == "6 PM":
        return ["6pm", "6-pm", "06-00", "6-00", "06pm", "6 pm"]
    if draw_label == "8 PM":
        return ["8pm", "8-pm", "08-00", "8-00", "08pm", "8 pm"]
    return []


def filter_by_draw(urls, draw_label):
    keys = draw_keywords(draw_label)
    matched = []
    others = []

    for url in urls:
        low = url.lower()
        if any(k in low for k in keys):
            matched.append(url)
        else:
            others.append(url)

    return matched + others


def extract_sources(html, page_url, draw_label):
    pdfs = []
    posters = []
    soup = BeautifulSoup(html, "html.parser")

    # Full direct URLs inside HTML/script/json
    pdfs.extend(
        re.findall(
            r'https?://[^\s"\'<>]+?\.pdf(?:\?[^\s"\'<>]*)?',
            html,
            flags=re.I,
        )
    )
    posters.extend(
        re.findall(
            r'https?://[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?',
            html,
            flags=re.I,
        )
    )

    # Relative WordPress uploads links
    relative_files = re.findall(
        r'(?:/wp-content/uploads/[^\s"\'<>]+?\.(?:pdf|jpg|jpeg|png|webp)(?:\?[^\s"\'<>]*)?)',
        html,
        flags=re.I,
    )

    for item in relative_files:
        full = urljoin(page_url, item)
        low = full.lower()
        if ".pdf" in low:
            pdfs.append(full)
        else:
            posters.append(full)

    # All possible HTML attributes
    for tag in soup.find_all(True):
        for attr in [
            "href",
            "src",
            "data-src",
            "data-lazy-src",
            "data-original",
            "data-full-url",
            "data-large_image",
            "data-bg",
            "data-background",
            "data-link",
            "data-url",
            "content",
        ]:
            value = tag.get(attr)
            if not value:
                continue

            full = urljoin(page_url, value.strip())
            low = full.lower()

            if ".pdf" in low:
                pdfs.append(full)

            if any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                posters.append(full)

        srcset = tag.get("srcset") or tag.get("data-srcset")
        if srcset:
            for part in srcset.split(","):
                src = part.strip().split(" ")[0].strip()
                if not src:
                    continue

                full = urljoin(page_url, src)
                low = full.lower()

                if any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                    posters.append(full)

    pdfs = unique_list([p for p in pdfs if ".pdf" in p.lower()])
    posters = unique_list([
        p for p in posters
        if any(ext in p.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
        and not is_bad_url(p)
    ])

    pdfs = filter_by_draw(pdfs, draw_label)
    posters = filter_by_draw(posters, draw_label)

    print(f"[CANDIDATES] pdf={len(pdfs)}, poster={len(posters)}", flush=True)
    print("[PDF]", pdfs[:10], flush=True)
    print("[POSTER]", posters[:10], flush=True)

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


def detect_result_date(text):
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


def find_first_prize(text):
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


def extract_prize_blocks(text):
    text = text.upper()

    second = ""
    third = ""
    fourth = ""
    fifth = ""

    m = re.search(r"2ND.*?(?:10000|10,000)(.*?)3RD", text, re.S)
    if m:
        second = m.group(1)

    m = re.search(r"3RD.*?500(.*?)4TH", text, re.S)
    if m:
        third = m.group(1)

    m = re.search(r"4TH.*?250(.*?)5TH", text, re.S)
    if m:
        fourth = m.group(1)

    m = re.search(r"5TH.*?120(.*)", text, re.S)
    if m:
        fifth = m.group(1)

    return {
        "second": second,
        "third": third,
        "fourth": fourth,
        "fifth": fifth,
    }


def build_parsed_data_from_text(text, source_name):
    parsed = empty_parsed_data()

    text = (text or "").upper()
    text = text.replace("₹", " ")
    text = text.replace(",", "")
    text = re.sub(r"[^\w\s:/.-]", " ", text)
    text = re.sub(r"\s+", " ", text)

    parsed["result_date"] = detect_result_date(text)

    first_series, first_number = find_first_prize(text)
    parsed["first_prize_series"] = first_series
    parsed["first_prize_number"] = first_number
    parsed["consolation_number"] = first_number
    parsed["parsed_source"] = source_name

    blocks = extract_prize_blocks(text)

    second_numbers = clean_unique_numbers(re.findall(r"\b\d{5}\b", blocks["second"]), 5)
    third_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", blocks["third"]), 4)
    fourth_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", blocks["fourth"]), 4)
    fifth_numbers = clean_unique_numbers(re.findall(r"\b\d{4}\b", blocks["fifth"]), 4)

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

    print("[PARSE OK]", flush=True)
    print(json.dumps(parsed, ensure_ascii=False, indent=2), flush=True)

    return parsed


def extract_pdf_text(content):
    doc = fitz.open(stream=content, filetype="pdf")
    full_text = ""

    for page in doc:
        full_text += "\n" + page.get_text("text")

    return full_text.upper()


def extract_lottery_data_from_pdf(content):
    try:
        text = extract_pdf_text(content)
        print("========== PDF TEXT PREVIEW ==========", flush=True)
        print(text[:3000], flush=True)
        print("========== PDF TEXT END ==========", flush=True)
        return build_parsed_data_from_text(text, "pdf_text")
    except Exception as e:
        print(f"[PDF PARSE ERROR] {e}", flush=True)
        return empty_parsed_data()


def preprocess_image_for_ocr(content):
    image = Image.open(io.BytesIO(content)).convert("RGB")

    width, height = image.size
    if width < 1600:
        scale = 1600 / width
        image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)

    image = ImageOps.grayscale(image)
    image = ImageEnhance.Contrast(image).enhance(2.5)
    image = ImageEnhance.Sharpness(image).enhance(2.0)
    image = image.filter(ImageFilter.SHARPEN)

    return image


def extract_lottery_data_from_image(content):
    try:
        image = preprocess_image_for_ocr(content)
        text = pytesseract.image_to_string(image, config="--oem 3 --psm 6")

        print("========== POSTER OCR TEXT PREVIEW ==========", flush=True)
        print(text[:3000], flush=True)
        print("========== POSTER OCR TEXT END ==========", flush=True)

        return build_parsed_data_from_text(text, "poster_ocr")
    except Exception as e:
        print(f"[POSTER OCR ERROR] {e}", flush=True)
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

    db.collection("Results").document(doc_id).set({
        "notification_sent": True,
        "notification_sent_at": firestore.SERVER_TIMESTAMP,
    }, merge=True)


def save_result_doc(date_str, draw_label, draw_code, pdf_url, poster_url, source_page, parsed_data, notification_sent):
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

    print(f"[FIRESTORE OK] Results/{doc_id}", flush=True)


def send_notification(date_str, draw_label, draw_code):
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
    print(f"[TOPIC NOTIFICATION SENT] {response}", flush=True)


def process_draw(default_date_str, draw_label, page_url):
    draw_code = DRAW_CODES[draw_label]

    print(f"\n==== {draw_label} ====", flush=True)
    print(f"[FETCH] {page_url}", flush=True)

    html = fetch_html(page_url)
    pdfs, posters = extract_sources(html, page_url, draw_label)

    date_str = default_date_str
    pdf_public_url = ""
    poster_public_url = ""
    parsed_data = empty_parsed_data()

    for pdf in pdfs:
        try:
            content, ext, content_type = download(pdf)
            print(f"[CHECK PDF] {pdf} | ext={ext} | size={len(content)}", flush=True)

            if ext == "pdf":
                temp_parsed = extract_lottery_data_from_pdf(content)
                detected_date = temp_parsed.get("result_date", "")

                if detected_date:
                    print(f"[PDF DATE] {detected_date}", flush=True)
                    date_str = detected_date

                parsed_data = temp_parsed

                path = f"results/{date_str}/{draw_code}_pdf.pdf"
                pdf_public_url = upload_to_storage(path, content, content_type)
                break

        except Exception as e:
            print(f"[PDF SKIP] {pdf} | {e}", flush=True)

    for poster in posters:
        try:
            content, ext, content_type = download(poster)
            print(f"[CHECK POSTER] {poster} | ext={ext} | size={len(content)}", flush=True)

            if ext in ["jpg", "png", "webp"] and len(content) > 3000:
                if not pdf_public_url:
                    temp_parsed = extract_lottery_data_from_image(content)
                    detected_date = temp_parsed.get("result_date", "")

                    if detected_date:
                        print(f"[POSTER DATE] {detected_date}", flush=True)
                        date_str = detected_date

                    parsed_data = temp_parsed

                path = f"results/{date_str}/{draw_code}_poster.{ext}"
                poster_public_url = upload_to_storage(path, content, content_type)
                break

        except Exception as e:
            print(f"[POSTER SKIP] {poster} | {e}", flush=True)

    if not pdf_public_url and not poster_public_url:
        print(f"[MISS] No valid PDF/poster for {draw_label}", flush=True)
        return False

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
        print(f"[NOTIFICATION SKIP] already sent for {date_str}_{draw_code}", flush=True)
    else:
        send_notification(date_str, draw_label, draw_code)
        mark_notification_sent(date_str, draw_code)

    return True


def run():
    print("SYNC FILE STARTED", flush=True)
    print("RUN FUNCTION STARTED", flush=True)

    global db, bucket

    print("[INIT FIREBASE]", flush=True)
    db, bucket = init_firebase()
    print("[INIT FIREBASE OK]", flush=True)

    date_str = today_date()
    allowed_draws = get_allowed_draws()
    success = 0

    print(f"[DATE] {date_str}", flush=True)
    print(f"[BUCKET] {BUCKET_NAME}", flush=True)
    print(f"[ALLOWED DRAWS] {allowed_draws}", flush=True)

    if not allowed_draws:
        print("[SKIP] Current time is not result sync time", flush=True)
        return

    for draw_label, page_urls in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        if draw_code not in allowed_draws:
            print(f"[SKIP DRAW] {draw_label}", flush=True)
            continue

        ok = False

        for page_url in page_urls:
            try:
                ok = process_draw(date_str, draw_label, page_url)
                if ok:
                    break
            except Exception as e:
                import traceback
                print(f"[ERROR] {draw_label} | {page_url}: {e}", flush=True)
                traceback.print_exc()

        if ok:
            success += 1

    print(f"\n[DONE] synced={success}", flush=True)


if __name__ == "__main__":
    run()
