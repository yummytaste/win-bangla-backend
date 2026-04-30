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
from pypdf import PdfReader
from PIL import Image
import pytesseract

# 🔥 MULTI SOURCE
DRAW_SOURCES = {
    "1 PM": [
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
    ],
    "6 PM": [
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
    ],
    "8 PM": [
        "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-8-pm.html",
    ],
}

DRAW_CODES = {"1 PM": "1PM", "6 PM": "6PM", "8 PM": "8PM"}

firebase_key = os.environ.get("FIREBASE_KEY")

if firebase_key:
    cred = credentials.Certificate(json.loads(firebase_key))
else:
    cred = credentials.Certificate("serviceAccountKey.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred, {"storageBucket": "grozip-pro.firebasestorage.app"})

db = firestore.client()
bucket = storage.bucket()

def today():
    return datetime.now().strftime("%Y-%m-%d")

def fetch(url):
    return requests.get(url, timeout=15).text

def download(url):
    return requests.get(url, timeout=20).content

def is_pdf(content):
    return content[:4] == b"%PDF"

def upload(path, content):
    blob = bucket.blob(path)
    blob.upload_from_string(content)
    blob.make_public()
    return blob.public_url

def ocr_image(content):
    img = Image.open(BytesIO(content))
    img = img.resize((img.width*2, img.height*2))
    return pytesseract.image_to_string(img)

def extract_numbers(text):
    return re.findall(r"\d{2}[A-Z]\d{5}", text.upper())

def already_done(date, code):
    doc = db.collection("results").document(f"{date}_{code}").get()
    return doc.exists and doc.to_dict().get("match_ready")

def save_result(date, label, code, nums):
    db.collection("results").document(f"{date}_{code}").set({
        "date": date,
        "draw_code": code,
        "numbers": nums,
        "match_ready": True,
        "updated_at": firestore.SERVER_TIMESTAMP
    }, merge=True)

    send_notification(label)

def send_notification(label):
    tokens = [d.to_dict()["token"] for d in db.collection("DeviceTokens").stream()]

    for t in tokens:
        try:
            messaging.send(messaging.Message(
                token=t,
                notification=messaging.Notification(
                    title="Result Published",
                    body=f"{label} result ready"
                )
            ))
        except:
            pass

def process_draw(date, label):
    code = DRAW_CODES[label]

    if already_done(date, code):
        print("SKIP", label)
        return True

    for source in DRAW_SOURCES[label]:
        try:
            html = fetch(source)
            soup = BeautifulSoup(html, "html.parser")

            # PDF FIRST
            for a in soup.find_all("a", href=True):
                if ".pdf" in a["href"]:
                    url = urljoin(source, a["href"])
                    content = download(url)

                    if is_pdf(content):
                        text = PdfReader(BytesIO(content)).pages[0].extract_text() or ""
                        nums = extract_numbers(text)

                        upload(f"results/{date}/{code}_pdf.pdf", content)

                        if nums:
                            save_result(date, label, code, nums)
                            return True

            # POSTER OCR
            for img in soup.find_all("img", src=True):
                src = urljoin(source, img["src"])
                content = download(src)

                if len(content) > 50000:
                    text = ocr_image(content)
                    nums = extract_numbers(text)

                    upload(f"results/{date}/{code}_poster.jpg", content)

                    if nums:
                        save_result(date, label, code, nums)
                        return True

        except Exception as e:
            print("ERROR:", e)

    return False

def run():
    date = today()

    for label in DRAW_SOURCES.keys():
        for i in range(6):
            print(f"{label} TRY {i+1}")

            if process_draw(date, label):
                break

            time.sleep(10)

if __name__ == "__main__":
    run()
