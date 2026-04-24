import os
import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore, storage, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

BASE_URL = "https://lotterysambadresult.in/"
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "serviceAccountKey.json")
BUCKET_NAME = "grozip-pro.firebasestorage.app"

DRAW_PAGES = {
    "1 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
    "6 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
    "8 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-8-pm.html",
}

DRAW_CODES = {"1 PM": "1PM", "6 PM": "6PM", "8 PM": "8PM"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": BASE_URL}

cred = credentials.Certificate(SERVICE_ACCOUNT_FILE)
firebase_admin.initialize_app(cred, {"storageBucket": BUCKET_NAME})

db = firestore.client()
bucket = storage.bucket()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def format_firestore_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


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
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def download_file(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    content = resp.content
    ext, content_type = detect_file_type(content)

    if ext is not None:
        return content, ext, content_type

    header_type = (resp.headers.get("Content-Type") or "").lower()
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


def save_result_doc(date_str, draw_label, draw_code, poster_storage_path, poster_url, poster_type, pdf_storage_path, pdf_url, source_page):
    doc_id = f"{date_str}_{draw_code}"
    doc_ref = db.collection("results").document(doc_id)
    existing = doc_ref.get()
    already_exists = existing.exists

    created_at_value = firestore.SERVER_TIMESTAMP
    if already_exists:
        old_data = existing.to_dict() or {}
        created_at_value = old_data.get("created_at", firestore.SERVER_TIMESTAMP)

    doc_ref.set({
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
    }, merge=True)

    return not already_exists


def log_sync(success: bool, message: str):
    db.collection("sync_logs").add({
        "job_name": "lotterysambadresult_sync",
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
            print(f"[WARN] Notification failed for token: {token[:20]}... -> {e}")
            invalid_tokens.append(token)

    for token in invalid_tokens:
        db.collection("DeviceTokens").document(token).set({
            "is_active": False,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }, merge=True)

    print(f"[INFO] Notification sent to {success_count} device(s) for {draw_label}")


def looks_like_matching_draw_page(page_url: str, draw_label: str) -> bool:
    page_url = page_url.lower()
    if draw_label == "1 PM":
        return "1-pm" in page_url
    if draw_label == "6 PM":
        return "6-pm" in page_url
    if draw_label == "8 PM":
        return "8-pm" in page_url
    return False


def extract_best_poster_and_pdf(page_html: str, page_url: str, draw_label: str):
    if not looks_like_matching_draw_page(page_url, draw_label):
        return None, None

    soup = BeautifulSoup(page_html, "html.parser")
    poster_url = None
    pdf_url = None

    for img in soup.find_all("img", src=True):
        src = urljoin(page_url, img.get("src", "").strip())
        alt = normalize_text(img.get("alt", ""))
        parent_text = normalize_text(img.parent.get_text(" ", strip=True) if img.parent else "")
        combined = f"{src.lower()} {alt} {parent_text}"

        if "coming" in combined or "soon" in combined:
            continue

        if "winner" in combined or "result" in combined or "lottery" in combined or "dear" in combined:
            poster_url = src
            break

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a.get("href", "").strip())
        text = normalize_text(a.get_text(" ", strip=True))
        parent_text = normalize_text(a.parent.get_text(" ", strip=True) if a.parent else "")
        combined = f"{href.lower()} {text} {parent_text}"

        if "download" in combined or href.lower().endswith(".pdf"):
            pdf_url = href
            break

    return poster_url, pdf_url


def sync_for_today():
    today = datetime.now()
    date_str = format_firestore_date(today)
    synced = 0

    for draw_label, page_url in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        try:
            page_html = fetch_page(page_url)
            poster_source_url, pdf_source_url = extract_best_poster_and_pdf(page_html, page_url, draw_label)

            poster_storage_path = None
            poster_public_url = None
            poster_type = None
            pdf_storage_path = None
            pdf_public_url = None

            if poster_source_url:
                content, ext, content_type = download_file(poster_source_url)
                if ext in ("jpg", "png", "webp"):
                    poster_storage_path, poster_public_url = upload_to_storage(
                        date_str, draw_code, content, ext, content_type, "poster"
                    )
                    poster_type = ext

            if pdf_source_url:
                content, ext, content_type = download_file(pdf_source_url)
                if ext == "pdf":
                    pdf_storage_path, pdf_public_url = upload_to_storage(
                        date_str, draw_code, content, ext, content_type, "pdf"
                    )

            if not poster_public_url and not pdf_public_url:
                print(f"[MISS] {date_str} {draw_label} -> no valid {draw_label} result found")
                continue

            is_new_doc = save_result_doc(
                date_str,
                draw_label,
                draw_code,
                poster_storage_path,
                poster_public_url,
                poster_type,
                pdf_storage_path,
                pdf_public_url,
                page_url,
            )

            synced += 1
            print(f"[OK] {date_str} {draw_label} -> poster={poster_public_url or 'none'} | pdf={pdf_public_url or 'none'}")

            if is_new_doc:
                send_result_notification(date_str, draw_label)

        except Exception as e:
            print(f"[ERROR] {date_str} {draw_label} -> {e}")

    log_sync(True, f"{date_str}: synced {synced} result(s)")


if __name__ == "__main__":
    try:
        sync_for_today()
    except Exception as e:
        today = datetime.now()
        log_sync(False, f"{format_firestore_date(today)} failed: {e}")
        print(f"[FAIL] {today.date()} -> {e}")
