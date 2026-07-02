"""
Samsun'daki dis klinigi/dis hekimi web sitelerinin altyapisini (CMS/framework)
tespit eden script.

Isletme kesfi iki yontemin BIRLESIMI:
  - Anahtar kelime + ilce metniyle Text Search
  - Samsun genelini sabit araliklarla (grid) noktalara bolup her noktada
    Nearby Search (type=dentist) yapmak. Yogun bolgelerde (60 sonuc
    tavanina carpilirsa) o nokta otomatik olarak kucuk yaricapli alt
    noktalara bolunur (quad-tree mantigi) - bkz. refine_search().
Iki yontemin sonuclari place_id'ye gore tekillestirilip birlestirilir.

Tum agir islemler PARALEL calisir (ThreadPoolExecutor): Text Search
sorgulari, Grid/Nearby Search noktalari, Place Details + website altyapi
taramasi.

Kullanim:
    pip install -r requirements.txt
    export GOOGLE_PLACES_API_KEY="senin-api-keyin"
    python3 api_versiyon.py

Cikti:
    samsun_disciler_altyapi.csv
    index.html
"""

import concurrent.futures
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime

import requests

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
OUTPUT_CSV = "samsun_disciler_altyapi.csv"
TEMPLATE_FILE = "dashboard_template.html"
OUTPUT_DASHBOARD = "index.html"
REQUEST_TIMEOUT = 10

TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Paralellik ayarlari
TEXT_SEARCH_WORKERS = 10
GRID_WORKERS = 8
DETAILS_SCAN_WORKERS = 12

# ---------------------------------------------------------------------------
# YONTEM 1: anahtar kelime + ilce metniyle Text Search
# ---------------------------------------------------------------------------

SAMSUN_ILCELERI = [
    "Atakum", "İlkadım", "Canik", "Tekkeköy", "Ondokuzmayıs", "Bafra",
    "Çarşamba", "Terme", "Salıpazarı", "Ayvacık", "Vezirköprü", "Havza",
    "Ladik", "Kavak", "Yakakent", "Alaçam", "Asarcık",
]

# Genisletilmis anahtar kelime listesi: genel terimlerin yanina uzmanlik
# alani terimleri eklendi (bircok tek hekimli / uzmanlik klinigi sadece
# bu terimlerle kayitli olabiliyor).
KEYWORDS = [
    "diş kliniği", "diş hekimi", "ağız ve diş sağlığı polikliniği",
    "implant merkezi", "ortodonti", "gülüş tasarımı", "ağız ve çene cerrahisi",
    "diş polikliniği", "pedodonti", "periodontoloji", "endodonti",
    "protetik diş tedavisi", "diş estetiği", "zirkonyum kaplama",
    "diş beyazlatma", "ağız diş sağlığı merkezi", "dental klinik",
]

SEARCH_QUERIES = [f"{kw} Samsun" for kw in KEYWORDS] + [
    f"{kw} {ilce} Samsun" for ilce in SAMSUN_ILCELERI for kw in KEYWORDS
]

# ---------------------------------------------------------------------------
# YONTEM 2: grid + Nearby Search (type=dentist), yogun bolgede otomatik bolme
# ---------------------------------------------------------------------------

LAT_MIN, LAT_MAX = 40.75, 41.95
LON_MIN, LON_MAX = 34.53, 37.05
GRID_RADIUS_M = 13000
LAT_STEP = 0.16
LON_STEP = 0.22
REFINE_MAX_DEPTH = 4
REFINE_MIN_RADIUS_M = 1000


def build_grid():
    points = []
    lat = LAT_MIN
    while lat <= LAT_MAX:
        lon = LON_MIN
        while lon <= LON_MAX:
            points.append((round(lat, 4), round(lon, 4)))
            lon += LON_STEP
        lat += LAT_STEP
    return points


def nearby_search(lat, lon, radius_m):
    """Bir noktada type=dentist ile Nearby Search yapar, gerekirse sayfalar."""
    results = []
    params = {
        "location": f"{lat},{lon}",
        "radius": radius_m,
        "type": "dentist",
        "language": "tr",
        "key": API_KEY,
    }
    page = 0
    while True:
        resp = requests.get(NEARBY_URL, params=params, timeout=REQUEST_TIMEOUT).json()
        status = resp.get("status")

        retries = 0
        while status == "INVALID_REQUEST" and retries < 4:
            time.sleep(2 + retries)
            resp = requests.get(NEARBY_URL, params=params, timeout=REQUEST_TIMEOUT).json()
            status = resp.get("status")
            retries += 1

        if status not in ("OK", "ZERO_RESULTS"):
            print(f"  Nearby hatasi ({lat},{lon}): {status} {resp.get('error_message', '')}", file=sys.stderr)
            break

        results.extend(resp.get("results", []))
        token = resp.get("next_page_token")
        page += 1
        if not token or page >= 3:
            break

        time.sleep(2)
        params = {"pagetoken": token, "key": API_KEY}

    return results


def refine_search(lat, lon, radius_m, depth=0, max_depth=REFINE_MAX_DEPTH, min_radius_m=REFINE_MIN_RADIUS_M):
    """60 sonuc tavanina carpilan yogun noktalari kucuk yaricapli 4 alt
    noktaya bolup tekrar dener (quad-tree). Bos bolgelerde devreye girmez."""
    places = nearby_search(lat, lon, radius_m)

    if len(places) < 60 or depth >= max_depth or radius_m <= min_radius_m:
        return places

    sub_radius = max(int(radius_m * 0.6), min_radius_m)
    offset_km = (radius_m / 2) / 1000
    lat_off = offset_km / 111.0
    lon_off = offset_km / (111.0 * math.cos(math.radians(lat)))

    combined = []
    for dlat, dlon in [(1, 1), (1, -1), (-1, 1), (-1, -1)]:
        combined.extend(refine_search(
            round(lat + dlat * lat_off, 5),
            round(lon + dlon * lon_off, 5),
            sub_radius, depth + 1, max_depth, min_radius_m,
        ))
    return combined


# ---------------------------------------------------------------------------
# Text Search (yontem 1) - sorgu basina sonuc toplama
# ---------------------------------------------------------------------------

def get_places_text(query):
    places = []
    params = {"query": query, "key": API_KEY, "language": "tr"}
    page = 0

    while True:
        resp = requests.get(TEXT_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT).json()
        status = resp.get("status")

        retries = 0
        while status == "INVALID_REQUEST" and retries < 4:
            time.sleep(2 + retries)
            resp = requests.get(TEXT_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT).json()
            status = resp.get("status")
            retries += 1

        if status not in ("OK", "ZERO_RESULTS"):
            print(f"  Text Search hatasi ({query}): {json.dumps(resp, ensure_ascii=False)}", file=sys.stderr)
            break

        places.extend(resp.get("results", []))
        token = resp.get("next_page_token")
        page += 1
        if not token or page >= 3:
            break

        time.sleep(2)
        params = {"pagetoken": token, "key": API_KEY}

    return places


# ---------------------------------------------------------------------------
# Ortak: adres -> ilce cikarma, teknoloji imzalari, site tarama
# ---------------------------------------------------------------------------

def extract_district(address):
    parts = [p.strip() for p in (address or "").split(",")]
    for part in reversed(parts):
        if "/samsun" in part.lower():
            district = part.split("/")[0].strip()
            district = re.sub(r"^\d{4,6}\s+", "", district)
            return district or "Diğer"
    return "Diğer"


FINGERPRINTS = {
    "WordPress": [r"wp-content", r"wp-includes", r"wp-json"],
    "Wix": [r"static\.wixstatic\.com", r"_wixCssMd5", r"wix\.com"],
    "Shopify": [r"cdn\.shopify\.com", r"Shopify\.theme"],
    "Squarespace": [r"squarespace\.com", r"static1\.squarespace\.com"],
    "Webflow": [r"webflow\.com", r"data-wf-site"],
    "Joomla": [r"/media/jui/", r"Joomla!"],
    "Drupal": [r"Drupal\.settings", r"/sites/default/files"],
    "Next.js": [r"__NEXT_DATA__", r"_next/static"],
    "React": [r"data-reactroot", r"react-dom"],
    "Angular": [r"ng-version"],
    "Vue.js": [r"data-v-", r"__vue__"],
    "Laravel": [r"laravel_session", r"XSRF-TOKEN"],
    "ASP.NET": [r"__VIEWSTATE", r"asp\.net"],
}


def get_place_details(place_id):
    params = {
        "place_id": place_id,
        "fields": "name,website,formatted_address",
        "key": API_KEY,
    }
    resp = requests.get(DETAILS_URL, params=params, timeout=REQUEST_TIMEOUT).json()
    return resp.get("result", {})


def detect_stack(url):
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TechDetectBot/1.0)"},
        )
    except requests.RequestException as exc:
        return {"detected": "", "server_header": "", "x_powered_by": "", "status_code": "", "error": str(exc)}

    haystack = resp.text + str(resp.headers)
    found = set()
    for tech, patterns in FINGERPRINTS.items():
        for pattern in patterns:
            if re.search(pattern, haystack, re.IGNORECASE):
                found.add(tech)
                break

    return {
        "detected": ", ".join(sorted(found)) or "Bilinmiyor / özel kod",
        "server_header": resp.headers.get("Server", ""),
        "x_powered_by": resp.headers.get("X-Powered-By", ""),
        "status_code": resp.status_code,
        "error": "",
    }


# ---------------------------------------------------------------------------
# Kesif: iki yontemi paralel calistir + birlestir
# ---------------------------------------------------------------------------

def collect_places():
    unique = {}

    # --- Yontem 1: Text Search, paralel ---
    print(f"[1/2] Text Search: {len(SEARCH_QUERIES)} sorgu, {TEXT_SEARCH_WORKERS} paralel worker")
    text_found = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=TEXT_SEARCH_WORKERS) as executor:
        futures = {executor.submit(get_places_text, q): q for q in SEARCH_QUERIES}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            query = futures[future]
            try:
                places = future.result()
            except Exception as exc:
                print(f"  İstisna ({query}): {exc}", file=sys.stderr)
                continue
            for place in places:
                if "samsun" not in place.get("formatted_address", "").lower():
                    continue
                if place["place_id"] not in unique:
                    text_found += 1
                unique.setdefault(place["place_id"], place)
            if done % 20 == 0 or done == len(SEARCH_QUERIES):
                print(f"  [{done}/{len(SEARCH_QUERIES)}] Text Search tamamlandi, su ana kadar: {len(unique)}")

    print(f"Text Search'ten gelen benzersiz: {text_found}")

    # --- Yontem 2: Grid + Nearby Search, paralel ---
    grid = build_grid()
    print(f"\n[2/2] Grid + Nearby Search: {len(grid)} nokta, {GRID_WORKERS} paralel worker")
    grid_found = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=GRID_WORKERS) as executor:
        futures = {executor.submit(refine_search, lat, lon, GRID_RADIUS_M): (lat, lon) for lat, lon in grid}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            lat, lon = futures[future]
            try:
                places = future.result()
            except Exception as exc:
                print(f"  İstisna ({lat},{lon}): {exc}", file=sys.stderr)
                continue
            for place in places:
                if place["place_id"] not in unique:
                    grid_found += 1
                unique.setdefault(place["place_id"], place)
            if done % 20 == 0 or done == len(grid):
                print(f"  [{done}/{len(grid)}] grid tamamlandi, su ana kadar toplam: {len(unique)}")

    print(f"Grid Nearby Search'ten gelen (Text Search'te olmayan) benzersiz: {grid_found}")
    print(f"\nToplam benzersiz isletme (birlesik): {len(unique)}")

    # Grid sonuclari formatted_address icermeyebilir (Nearby Search sadece
    # 'vicinity' dondurur) - Samsun disi il sizintisini eleme islemi zaten
    # asagida Place Details asamasinda formatted_address ile yapilacak.
    return list(unique.values())


def build_rows(places):
    print(f"\nPlace Details + website taramasi: {len(places)} isletme, {DETAILS_SCAN_WORKERS} paralel worker")

    def process(place):
        details = get_place_details(place["place_id"])
        website = details.get("website", "")
        address = details.get("formatted_address") or place.get("formatted_address", "") or place.get("vicinity", "")

        row = {
            "name": details.get("name") or place.get("name", ""),
            "address": address,
            "district": extract_district(address),
            "website": website,
            "detected": "",
            "server_header": "",
            "x_powered_by": "",
            "status_code": "",
            "error": "",
        }

        if website:
            row.update(detect_stack(website))
        else:
            row["detected"] = "Website yok"

        return row

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAILS_SCAN_WORKERS) as executor:
        futures = [executor.submit(process, p) for p in places]
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            try:
                rows.append(future.result())
            except Exception as exc:
                print(f"  İstisna: {exc}", file=sys.stderr)
            if done % 30 == 0 or done == len(places):
                print(f"  [{done}/{len(places)}] taramalar tamamlandi")

    # Samsun disi il sizintisini burada eleyelim (adres artik kesin biliniyor).
    rows = [r for r in rows if "samsun" in r["address"].lower()]
    return rows


def write_csv(rows, path):
    fieldnames = ["name", "address", "district", "website", "detected", "server_header", "x_powered_by", "status_code", "error"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_dashboard(rows, path, template_path, query_count):
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    dashboard_rows = [
        {
            "name": r["name"],
            "address": r["address"],
            "district": r["district"],
            "website": r["website"],
            "detected": r["detected"],
            "error": r["error"],
        }
        for r in rows
    ]

    meta = {
        "generatedAt": datetime.now().strftime("%d.%m.%Y %H:%M"),
        "queryCount": query_count,
    }

    html = template.replace(
        "/*__DATA__*/[]", json.dumps(dashboard_rows, ensure_ascii=False)
    ).replace(
        "/*__META__*/{\"generatedAt\": \"\", \"queryCount\": 0}", json.dumps(meta, ensure_ascii=False)
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    if not API_KEY:
        sys.exit("Hata: GOOGLE_PLACES_API_KEY ortam değişkeni tanımlı değil.")

    if not os.path.exists(TEMPLATE_FILE):
        sys.exit(f"Hata: {TEMPLATE_FILE} bulunamadı. Script ile aynı klasörde olmalı.")

    start = time.time()

    places = collect_places()
    rows = build_rows(places)

    write_csv(rows, OUTPUT_CSV)
    print(f"\nCSV yazıldı: {OUTPUT_CSV} ({len(rows)} satır)")

    total_query_count = len(SEARCH_QUERIES) + len(build_grid())
    write_dashboard(rows, OUTPUT_DASHBOARD, TEMPLATE_FILE, total_query_count)
    print(f"Panel oluşturuldu: {OUTPUT_DASHBOARD}")

    elapsed = time.time() - start
    print(f"\nToplam süre: {elapsed / 60:.1f} dakika")


if __name__ == "__main__":
    main()
