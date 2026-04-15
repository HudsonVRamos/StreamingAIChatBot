import os
import requests
from bs4 import BeautifulSoup
import pdfkit
from urllib.parse import urljoin

BASE_URL = "https://docs.unified-streaming.com/documentation/"
OUTPUT_DIR = "pdf_docs"

visited = set()
to_visit = set([BASE_URL])

os.makedirs(OUTPUT_DIR, exist_ok=True)


def is_valid(url):
    return "docs.unified-streaming.com/documentation" in url


def sanitize(url):
    return url.replace("https://", "").replace("/", "_")


while to_visit:
    url = to_visit.pop()

    if url in visited:
        continue

    print(f"[INFO] {url}")
    visited.add(url)

    try:
        res = requests.get(url)
        soup = BeautifulSoup(res.text, "html.parser")

        for a in soup.find_all("a", href=True):
            link = urljoin(url, a["href"])
            if is_valid(link) and link not in visited:
                to_visit.add(link)

        filename = os.path.join(OUTPUT_DIR, sanitize(url) + ".pdf")

        pdfkit.from_url(url, filename)

        print(f"[OK] {filename}")

    except Exception as e:
        print(f"[ERRO] {url} -> {e}")