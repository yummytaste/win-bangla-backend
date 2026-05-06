import os
import re
import json
from datetime import datetime
from urllib.parse import urljoin

import requests
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
    response = requests.get(url, headers=HEADERS, timeout=40)
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
    response = requests.get(url, headers=HEADERS, timeout=30)
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

    image_attrs = ["src", "data-src", "data-lazy-src", "data-original"]

    for img in soup.find_all("img"):
        for attr in image_attrs:
            src = img.get(attr)

            if src:
                src = urljoin(page_url, src.strip())
                posters.append(src)

        srcset = img.get("srcset") or img.get("data-srcset")

        if srcset:
            for part in srcset.split(","):
                src = part.strip().split(" ")[0].strip()

                if src:
                    posters.append(urljoin(page_url, src))

    pdfs = unique_list(pdfs)
    posters = unique_list(posters)

    pdfs = [
        p for p in pdfs
        if ".pdf" in p.lower()
    ]

    posters = [
        p for p in posters
        if any(ext in p.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
        and not is_bad_url(p)
    ]

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


def save_result_doc(date_str, draw_label, draw_code, pdf_url, poster_url, source_page):
    doc_id = f"{date_str}_{draw_code}"

    data = {
        "date": date_str,
        "time": draw_label,
        "draw_code": draw_code,
        "pdf_url": pdf_url or "",
        "poster_url": poster_url or "",
        "download_url": pdf_url or poster_url or "",
        "source_page": source_page,
        "status": "available",
        "match_ready": True,
        "updated_at": firestore.SERVER_TIMESTAMP,
        "created_at": firestore.SERVER_TIMESTAMP,
    }

    db.collection("results").document(doc_id).set(data, merge=True)
    db.collection("Results").document(doc_id).set(data, merge=True)

    print(f"[FIRESTORE OK] results/{doc_id}")


def send_notification(date_str, draw_label, draw_code):
    tokens = []

    try:
        docs = db.collection("DeviceTokens").stream()

        for doc in docs:
            data = doc.to_dict() or {}
            token = data.get("token")

            if token:
                tokens.append(token)

    except Exception as e:
        print(f"[NOTIFICATION WARN] token read failed: {e}")
        return

    if not tokens:
        print("[NOTIFICATION] No tokens found")
        return

    sent = 0

    for token in tokens:
        try:
            msg = messaging.Message(
                token=token,
                notification=messaging.Notification(
                    title="WB Lottery Result – Live",
                    body=f"{draw_label} result published for {date_str}",
                ),
                data={
                    "type": "result_published",
                    "date": date_str,
                    "time": draw_label,
                    "draw_code": draw_code,
                },
            )

            messaging.send(msg)
            sent += 1

        except Exception as e:
            print(f"[NOTIFICATION WARN] {e}")

    print(f"[NOTIFICATION OK] sent={sent}")


def process_draw(date_str, draw_label, page_url):
    draw_code = DRAW_CODES[draw_label]

    print(f"\n==== {draw_label} ====")
    print(f"[FETCH] {page_url}")

    html = fetch_html(page_url)
    pdfs, posters = extract_sources(html, page_url)

    pdf_public_url = ""
    poster_public_url = ""

    for pdf in pdfs:
        try:
            content, ext, content_type = download(pdf)
            print(f"[CHECK PDF] {pdf} | ext={ext} | size={len(content)}")

            if ext == "pdf":
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
                break

        except Exception as e:
            print(f"[POSTER SKIP] {poster} | {e}")

    if not pdf_public_url and not poster_public_url:
        print(f"[MISS] No valid PDF/poster for {draw_label}")
        return False

    save_result_doc(
        date_str=date_str,
        draw_label=draw_label,
        draw_code=draw_code,
        pdf_url=pdf_public_url,
        poster_url=poster_public_url,
        source_page=page_url,
    )

    send_notification(date_str, draw_label, draw_code)

    return True


def run():
    print("SYNC FILE STARTED")
    print("RUN FUNCTION STARTED")

    date_str = today_date()
    success = 0

    print(f"[DATE] {date_str}")
    print(f"[BUCKET] {BUCKET_NAME}")

    for draw_label, page_url in DRAW_PAGES.items():
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
