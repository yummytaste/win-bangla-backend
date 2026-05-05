# ===================== IMPORT =====================
import os, re, json, time
from datetime import datetime
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore, storage, messaging

# ===================== CONFIG =====================
BASE_URL = "https://lotterysambadresult.in/"
BUCKET = "grozip-pro.firebasestorage.app"

DRAW_PAGES = {
    "1 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-1-pm.html",
    "6 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-6-pm.html",
    "8 PM": "https://lotterysambadresult.in/nagaland-state-lottery-sambad-today-result-8-pm.html",
}

DRAW_CODES = {"1 PM":"1PM","6 PM":"6PM","8 PM":"8PM"}

HEADERS = {"User-Agent":"Mozilla/5.0"}

# ===================== FIREBASE =====================
cred = credentials.Certificate(json.loads(os.environ["FIREBASE_KEY"]))
firebase_admin.initialize_app(cred, {"storageBucket": BUCKET})

db = firestore.client()
bucket = storage.bucket()

# ===================== HELPERS =====================
def today():
    return datetime.now().strftime("%Y-%m-%d")

def is_bad(url):
    bad = ["ed14","install","logo","app","banner","youtube"]
    url = url.lower()
    return any(x in url for x in bad)

# ===================== FETCH =====================
def fetch(url):
    return requests.get(url, headers=HEADERS).text

# ===================== EXTRACT =====================
def extract(page, base):
    pdfs, posters = [], []

    # PDF regex
    pdfs += re.findall(r'https?://[^"]+?\.pdf', page)

    # IMAGE regex
    posters += re.findall(r'https?://[^"]+?\.(?:jpg|jpeg|png|webp)', page)

    soup = BeautifulSoup(page, "html.parser")

    for a in soup.find_all("a", href=True):
        link = urljoin(base, a["href"])
        if ".pdf" in link:
            pdfs.append(link)

    for img in soup.find_all("img"):
        for attr in ["src","data-src"]:
            src = img.get(attr)
            if src:
                src = urljoin(base, src)
                posters.append(src)

    # CLEAN
    pdfs = list(set(pdfs))
    posters = list(set(posters))

    # FILTER BAD
    posters = [p for p in posters if not is_bad(p)]

    # PRIORITY SORT
    posters = sorted(posters, key=lambda x: (
        0 if "uploads/2026" in x else 1,
        0 if "img_" in x else 1
    ))

    print("PDF:", pdfs[:3])
    print("POSTER:", posters[:3])

    return pdfs, posters

# ===================== DOWNLOAD =====================
def get(url):
    r = requests.get(url, headers=HEADERS)
    return r.content

# ===================== UPLOAD =====================
def upload(path, data):
    blob = bucket.blob(path)
    blob.upload_from_string(data)
    blob.make_public()
    print("[STORAGE OK]", path)
    return blob.public_url

# ===================== MAIN =====================
def run():
    d = today()
    success = 0

    for name, url in DRAW_PAGES.items():
        print("\n====", name, "====")

        html = fetch(url)
        pdfs, posters = extract(html, url)

        pdf_url = pdfs[0] if pdfs else None
        poster_url = posters[0] if posters else None

        if not pdf_url and not poster_url:
            print("❌ no result")
            continue

        pdf_link = ""
        poster_link = ""

        if pdf_url:
            data = get(pdf_url)
            if data[:4] == b"%PDF":
                pdf_link = upload(f"results/{d}/{name}_pdf.pdf", data)

        if poster_url:
            data = get(poster_url)
            if len(data) > 5000:
                poster_link = upload(f"results/{d}/{name}_poster.webp", data)

        doc = {
            "date": d,
            "time": name,
            "pdf": pdf_link,
            "poster": poster_link,
            "created_at": firestore.SERVER_TIMESTAMP
        }

        db.collection("results").document(f"{d}_{name}").set(doc)

        print("[FIRESTORE OK]")
        success += 1

    print("\nDONE:", success)

# ===================== RUN =====================
if __name__ == "__main__":
    run()
