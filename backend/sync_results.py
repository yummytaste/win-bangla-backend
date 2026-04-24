import os
import re
import json
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore, storage, messaging
from google.cloud.firestore_v1.base_query import FieldFilter

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


# =========================
# FIREBASE INIT
# =========================
firebase_key = os.environ.get("FIREBASE_KEY")

if firebase_key:
    cred = credentials.Certificate(json.loads(firebase_key))
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred, {
    "storageBucket": BUCKET_NAME
})

db = firestore.client()
bucket = storage.bucket()


# =========================
# HELPERS
# =========================
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


def upload_to_storage(date_str: str, draw_code: str, content: bytes, ext: str, content_type: str, kind: str):
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


def save_result_doc(
    date_str: str,
    draw_label: str,
    draw_code: str,
    poster_storage_path: str | None,
    poster_url: str | None,
    poster_type: str | None,
    pdf_storage_path: str | None,
    pdf_url: str | None,
    source_page: str,
):
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
        "coming",
        "soon",
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

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a.get("href", "").strip())
        text = a.get_text(" ", strip=True)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else ""

        combined = f"{href} {text} {parent_text}"

        if is_bad_placeholder(combined):
            continue

        combined_norm = normalize_text(combined)

        if "download" in combined_norm or href.lower().endswith(".pdf"):
            pdf_url = href
            break

    if is_bad_placeholder(page_text) and not pdf_url:
        return None, None

    return poster_url, pdf_url


def sync_for_today():
    date_str = today_date()
    synced = 0

    for draw_label, page_url in DRAW_PAGES.items():
        draw_code = DRAW_CODES[draw_label]

        try:
            page_html = fetch_page(page_url)

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

            if poster_source_url:
                content, ext, content_type = download_file(poster_source_url)

                if ext in ("jpg", "png", "webp"):
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
                    pdf_storage_path, pdf_public_url = upload_to_storage(
                        date_str=date_str,
                        draw_code=draw_code,
                        content=content,
                        ext=ext,
                        content_type=content_type,
                        kind="pdf",
                    )

            if not poster_public_url and not pdf_public_url:
                print(f"[MISS] {date_str} {draw_label} -> no valid {draw_label} result found")
                continue

            is_new_doc = save_result_doc(
                date_str=date_str,
                draw_label=draw_label,
                draw_code=draw_code,
                poster_storage_path=poster_storage_path,
                poster_url=poster_public_url,
                poster_type=poster_type,
                pdf_storage_path=pdf_storage_path,
                pdf_url=pdf_public_url,
                source_page=page_url,
            )

            synced += 1

            print(
                f"[OK] {date_str} {draw_label} -> "
                f"poster={poster_public_url or 'none'} | "
                f"pdf={pdf_public_url or 'none'}"
            )

            if is_new_doc:
                send_result_notification(date_str, draw_label)

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
