"""
Dis klinigi/dis hekimi isletmelerini Google Places uzerinden kesfedip web
sitelerinin altyapisini (CMS/framework) tespit eden script.

KAPSAM (SCAN_SCOPE ortam degiskeni):
  - "samsun"     : sadece Samsun (varsayilan; haftalik cron bunu kullanir)
  - "karadeniz"  : PROVINCES sozlugundeki tum iller (aylik cron / [tam] push)
  - "Samsun,Ordu": virgullu il listesi de verilebilir

KESIF YONTEMLERI:
  1) Text Search: anahtar kelime x (il + ilceler). Samsun icin genis kelime
     listesi, diger iller icin cekirdek liste (maliyet kontrolu).
  2) Grid + Nearby Search (sadece Samsun): il sabit araliklarla noktalara
     bolunur, her noktada once type=dentist sonra keyword=dis taramasi
     yapilir. 60 sonuc tavanina carpan yogun noktalar kucuk yaricapli alt
     noktalara bolunur (quad-tree) - bkz. refine_search().

GUVENILIRLIK:
  - Tum API cagrilari OVER_QUERY_LIMIT / UNKNOWN_ERROR / ag hatalarinda
    ustel bekleme (exponential backoff) ile yeniden denenir.
  - Kalici olarak basarisiz olan sorgular calisma sonunda bir tur daha
    seri denenir ve ozet olarak raporlanir (sessiz veri kaybi olmaz).
  - REQUEST_DENIED (key sorunu) tum taramayi hemen durdurur ki eksik
    sonuc canli panele yazilmasin.

KALICI VERITABANI (places_db.json):
  Gorulen her isletme first_seen/last_seen tarihleriyle saklanir. Dar
  kapsamli bir tarama (or. haftalik Samsun) genis taramanin (aylik
  Karadeniz) sonuclarini SILMEZ; sadece kendi kapsamindaki kayitlari
  tazeler. 120 gundur gorulmeyen kayitlar dusurulur. "Yeni" etiketi,
  daha once taranmis bir ILDE ilk kez gorulen isletmeler icindir (yeni
  bir il kapsama eklendiginde o ilin tum isletmeleri "yeni" sayilmaz).

Kullanim:
    pip install -r requirements.txt
    export GOOGLE_PLACES_API_KEY="senin-api-keyin"
    SCAN_SCOPE=samsun python3 api_versiyon.py

Cikti:
    samsun_disciler_altyapi.csv
    index.html
    places_db.json
"""

import concurrent.futures
import csv
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta

import requests

API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY")
SCAN_SCOPE = os.environ.get("SCAN_SCOPE", "samsun").strip()
OUTPUT_CSV = "samsun_disciler_altyapi.csv"
TEMPLATE_FILE = "dashboard_template.html"
OUTPUT_DASHBOARD = "index.html"
PLACES_DB_FILE = "places_db.json"
REQUEST_TIMEOUT = 10
STALE_DAYS = 120  # bu kadar gundur gorulmeyen kayitlar dusurulur

TEXT_SEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Paralellik ayarlari
TEXT_SEARCH_WORKERS = 10
GRID_WORKERS = 8
DETAILS_SCAN_WORKERS = 16

# ---------------------------------------------------------------------------
# Il / ilce verisi
# ---------------------------------------------------------------------------
# Not: "Merkez" ilceler listede yok; il adiyla yapilan sorgu merkezi kapsar.

PROVINCES = {
    "Samsun": [
        "Atakum", "İlkadım", "Canik", "Tekkeköy", "Ondokuzmayıs", "Bafra",
        "Çarşamba", "Terme", "Salıpazarı", "Ayvacık", "Vezirköprü", "Havza",
        "Ladik", "Kavak", "Yakakent", "Alaçam", "Asarcık",
    ],
    "Sinop": ["Ayancık", "Boyabat", "Dikmen", "Durağan", "Erfelek", "Gerze", "Türkeli"],
    "Ordu": [
        "Altınordu", "Ünye", "Fatsa", "Perşembe", "Kumru", "Korgan", "Gölköy",
        "Ulubey", "Gürgentepe", "Mesudiye", "Akkuş", "Aybastı", "Çamaş",
        "Çatalpınar", "Çaybaşı", "İkizce", "Kabadüz", "Kabataş",
    ],
    "Giresun": [
        "Bulancak", "Espiye", "Görele", "Tirebolu", "Keşap", "Dereli",
        "Şebinkarahisar", "Alucra", "Yağlıdere", "Piraziz", "Eynesil",
    ],
    "Trabzon": [
        "Ortahisar", "Akçaabat", "Araklı", "Arsin", "Beşikdüzü", "Çarşıbaşı",
        "Çaykara", "Düzköy", "Maçka", "Of", "Sürmene", "Şalpazarı", "Tonya",
        "Vakfıkebir", "Yomra",
    ],
    "Rize": [
        "Ardeşen", "Çayeli", "Pazar", "Fındıklı", "Güneysu", "Derepazarı",
        "İyidere", "Kalkandere", "Çamlıhemşin",
    ],
    "Artvin": ["Hopa", "Arhavi", "Borçka", "Şavşat", "Yusufeli", "Ardanuç", "Kemalpaşa"],
    "Gümüşhane": ["Kelkit", "Şiran", "Torul", "Köse", "Kürtün"],
    "Bayburt": ["Aydıntepe", "Demirözü"],
    "Tokat": [
        "Erbaa", "Turhal", "Niksar", "Zile", "Almus", "Artova", "Pazar",
        "Reşadiye", "Yeşilyurt", "Başçiftlik",
    ],
    "Amasya": ["Merzifon", "Suluova", "Taşova", "Gümüşhacıköy", "Göynücek"],
    "Çorum": [
        "Sungurlu", "Osmancık", "İskilip", "Alaca", "Bayat", "Kargı",
        "Mecitözü", "Ortaköy", "Oğuzlar", "Uğurludağ",
    ],
    "Kastamonu": [
        "Tosya", "Taşköprü", "İnebolu", "Cide", "Araç", "Devrekani",
        "Bozkurt", "Çatalzeytin", "Daday", "Küre", "Abana", "Azdavay",
    ],
    "Karabük": ["Safranbolu", "Yenice", "Eskipazar", "Eflani", "Ovacık"],
    "Bartın": ["Amasra", "Ulus", "Kurucaşile"],
    "Zonguldak": ["Ereğli", "Çaycuma", "Devrek", "Alaplı", "Gökçebey", "Kilimli", "Kozlu"],
    "Düzce": ["Akçakoca", "Kaynaşlı", "Gölyaka", "Çilimli", "Cumayeri", "Gümüşova", "Yığılca"],
    "Bolu": ["Gerede", "Mengen", "Mudurnu", "Göynük", "Yeniçağa", "Dörtdivan", "Seben"],
}

# Samsun icin genis anahtar kelime listesi (uzmanlik terimleri dahil)
KEYWORDS_FULL = [
    "diş kliniği", "diş hekimi", "dişçi", "diş doktoru",
    "ağız ve diş sağlığı polikliniği", "ağız diş sağlığı merkezi",
    "diş polikliniği", "diş hastanesi", "dental klinik",
    "implant merkezi", "ortodonti", "gülüş tasarımı",
    "ağız ve çene cerrahisi", "pedodonti", "periodontoloji", "endodonti",
    "protetik diş tedavisi", "diş estetiği", "zirkonyum kaplama",
    "diş beyazlatma",
]

# Diger iller icin cekirdek liste (sorgu maliyetini kontrol altinda tutar;
# uzmanlik terimleri buyuk oranda ayni isletmeleri dondurur)
KEYWORDS_CORE = [
    "diş kliniği", "diş hekimi", "dişçi", "diş doktoru",
    "ağız ve diş sağlığı", "ortodonti", "implant diş",
]

# Grid + Nearby Search sadece Samsun icin (en yogun kapsam)
GRID_PROVINCE = "Samsun"
LAT_MIN, LAT_MAX = 40.75, 41.95
LON_MIN, LON_MAX = 34.53, 37.05
GRID_RADIUS_M = 13000
LAT_STEP = 0.16
LON_STEP = 0.22
REFINE_MAX_DEPTH = 4
REFINE_MIN_RADIUS_M = 1000
# Sayfalama tokeni bozuk key'lerde bir nokta en fazla 20 sonuc dondurebilir;
# bu yuzden bolme esigi 60 degil 20'dir (tam sayfa = muhtemelen kesilmis sonuc)
REFINE_THRESHOLD = 20

TODAY = datetime.now().strftime("%Y-%m-%d")


def resolve_scope():
    scope = SCAN_SCOPE.lower()
    if scope in ("samsun", ""):
        return ["Samsun"]
    if scope in ("karadeniz", "tam", "hepsi", "all"):
        return list(PROVINCES)
    ils = []
    for part in SCAN_SCOPE.split(","):
        part = part.strip()
        match = next((il for il in PROVINCES if tr_lower(il) == tr_lower(part)), None)
        if not match:
            sys.exit(f"Hata: bilinmeyen il '{part}'. Gecerli: {', '.join(PROVINCES)}")
        ils.append(match)
    return ils


def tr_lower(s):
    return (s or "").replace("İ", "i").replace("I", "ı").lower()


# ---------------------------------------------------------------------------
# API katmani: backoff'lu, kayip vermeyen istek fonksiyonlari
# ---------------------------------------------------------------------------

class TransientAPIError(Exception):
    """Tekrar denenip yine basarisiz olan istek."""


def api_get(url, params, tries=5):
    """Places API cagrisi. Gecici hatalarda (kota, ag, pagetoken isinmasi)
    ustel bekleyerek yeniden dener. REQUEST_DENIED tum taramayi durdurur."""
    delay = 2
    last_status = ""
    for attempt in range(tries):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT).json()
        except (requests.RequestException, ValueError) as exc:
            last_status = f"ag hatasi: {exc}"
            time.sleep(delay)
            delay = min(delay * 2, 32)
            continue

        status = resp.get("status")
        if status in ("OK", "ZERO_RESULTS"):
            return resp
        if status == "REQUEST_DENIED":
            sys.exit(f"Hata: REQUEST_DENIED - API key sorunu: {resp.get('error_message', '')}")
        if status == "INVALID_REQUEST" and "pagetoken" not in params:
            # pagetoken disinda INVALID_REQUEST kalicidir, denemeye deger degil
            raise TransientAPIError(f"INVALID_REQUEST: {json.dumps(params, ensure_ascii=False)}")

        # OVER_QUERY_LIMIT / UNKNOWN_ERROR / pagetoken isinmasi -> bekle, tekrar dene
        last_status = status
        time.sleep(delay)
        delay = min(delay * 2, 32)

    raise TransientAPIError(f"{tries} denemede basarisiz ({last_status})")


PAGETOKEN_BROKEN = False


def paged_search(url, params, max_pages=3):
    """api_get ile sayfalanmis arama; en fazla max_pages sayfa (60 sonuc).

    Yeni API key'lerde legacy API'nin next_page_token'i kalici olarak
    INVALID_REQUEST donebiliyor. Bu durumda eldeki sayfalarin sonuclari
    ASLA cope atilmaz; toplanan sonuclarla devam edilir."""
    global PAGETOKEN_BROKEN
    results = []
    page = 0
    while True:
        try:
            # pagetoken istekleri icin sabir kisa tutulur: token bozuksa
            # (yeni key'lerde yaygin) uzun backoff sadece zaman kaybi
            resp = api_get(url, params, tries=3 if "pagetoken" in params else 5)
        except TransientAPIError:
            if page == 0:
                raise  # ilk sayfa bile alinamadi -> sorgu gercekten basarisiz
            PAGETOKEN_BROKEN = True
            return results  # sayfalama coktu; ilk sayfa(lar) elimizde kalsin
        results.extend(resp.get("results", []))
        token = resp.get("next_page_token")
        page += 1
        if not token or page >= max_pages:
            return results
        time.sleep(2)
        params = {"pagetoken": token, "key": API_KEY}


# ---------------------------------------------------------------------------
# Kesif yontemleri
# ---------------------------------------------------------------------------

def get_places_text(query):
    return paged_search(TEXT_SEARCH_URL, {"query": query, "key": API_KEY, "language": "tr"})


def nearby_search(lat, lon, radius_m, mode="type"):
    """Bir noktada Nearby Search. mode='type' -> type=dentist,
    mode='keyword' -> keyword=diş (kategorisi yanlis girilmis yerler icin)."""
    params = {"location": f"{lat},{lon}", "radius": radius_m, "language": "tr", "key": API_KEY}
    if mode == "type":
        params["type"] = "dentist"
    else:
        params["keyword"] = "diş"
    return paged_search(NEARBY_URL, params)


def refine_search(lat, lon, radius_m, mode="type", depth=0):
    """60 sonuc tavanina carpan yogun noktalari 4 alt noktaya bolup tekrar
    dener (quad-tree). Bos bolgelerde devreye girmez."""
    places = nearby_search(lat, lon, radius_m, mode)

    if len(places) < REFINE_THRESHOLD or depth >= REFINE_MAX_DEPTH or radius_m <= REFINE_MIN_RADIUS_M:
        return places

    sub_radius = max(int(radius_m * 0.6), REFINE_MIN_RADIUS_M)
    offset_km = (radius_m / 2) / 1000
    lat_off = offset_km / 111.0
    lon_off = offset_km / (111.0 * math.cos(math.radians(lat)))

    combined = []
    for dlat, dlon in [(1, 1), (1, -1), (-1, 1), (-1, -1)]:
        combined.extend(refine_search(
            round(lat + dlat * lat_off, 5),
            round(lon + dlon * lon_off, 5),
            sub_radius, mode, depth + 1,
        ))
    return combined


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


def build_text_queries(active_ils):
    queries = []
    for il in active_ils:
        keywords = KEYWORDS_FULL if il == "Samsun" else KEYWORDS_CORE
        queries.extend(f"{kw} {il}" for kw in keywords)
        queries.extend(f"{kw} {ilce} {il}" for ilce in PROVINCES[il] for kw in keywords)
    return queries


# 'diş' kelimesi 'dişli' (sanayi) ile karismasin diye negatif bakis kullanilir
DENTAL_NAME_RE = re.compile(r"diş(?!li)|dent|orto|implant|pedodon|periodon|endodon")
DENTAL_TYPES = {"dentist", "doctor", "health", "hospital"}


def looks_dental(place):
    """keyword=diş grid gecisi tip filtresi olmadan calisir; alakasiz
    isletmeleri (or. adinda 'dişli' gecen sanayi dukkanlari) eler."""
    if DENTAL_TYPES & set(place.get("types", [])):
        return True
    return bool(DENTAL_NAME_RE.search(tr_lower(place.get("name", ""))))


# ---------------------------------------------------------------------------
# Adres -> il/ilce cikarma
# ---------------------------------------------------------------------------

IL_PATTERN = re.compile(
    r"([^,/]+)/\s*(" + "|".join(re.escape(il) for il in PROVINCES) + r")\b",
    re.IGNORECASE,
)


def extract_location(address):
    """'... , 55270 Atakum/Samsun, Türkiye' -> ('Samsun', 'Atakum').
    Il PROVINCES listesinde degilse (None, None) doner (il sizintisi)."""
    m = IL_PATTERN.search(address or "")
    if not m:
        return None, None
    district = re.sub(r"^\s*\d{4,6}\s+", "", m.group(1)).strip()
    il_raw = m.group(2)
    il = next((p for p in PROVINCES if tr_lower(p) == tr_lower(il_raw)), None)
    return il, (district or "Diğer")


# ---------------------------------------------------------------------------
# Teknoloji imzalari, site tarama, Place Details
# ---------------------------------------------------------------------------

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
    resp = api_get(DETAILS_URL, {
        "place_id": place_id,
        "fields": "name,website,formatted_address,rating,user_ratings_total",
        "key": API_KEY,
    })
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
# Kesif: tum yontemleri paralel calistir, basarisizlari raporla
# ---------------------------------------------------------------------------

def run_parallel(tasks, workers, label):
    """tasks: {aciklama: callable}. Basarili sonuclari (liste listesi) ve
    basarisiz gorev aciklamalarini dondurur."""
    results, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fn): desc for desc, fn in tasks.items()}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            done += 1
            desc = futures[future]
            try:
                results.append(future.result())
            except TransientAPIError as exc:
                print(f"  BASARISIZ ({desc}): {exc}", file=sys.stderr)
                failed.append(desc)
            except Exception as exc:
                print(f"  Istisna ({desc}): {exc}", file=sys.stderr)
                failed.append(desc)
            if done % 25 == 0 or done == len(futures):
                print(f"  [{done}/{len(futures)}] {label}")
    return results, failed


def collect_places(active_ils):
    unique = {}
    active_lower = [tr_lower(il) for il in active_ils]

    def add(place, require_dental_hint=False):
        if require_dental_hint and not looks_dental(place):
            return
        # Text Search adres dondurur; hizli il filtresi (kesin filtre Details
        # asamasinda). Nearby sonuclarinda formatted_address yoktur -> gecer.
        addr = tr_lower(place.get("formatted_address", ""))
        if addr and not any(il in addr for il in active_lower):
            return
        unique.setdefault(place["place_id"], place)

    # --- Yontem 1: Text Search ---
    queries = build_text_queries(active_ils)
    print(f"[1/2] Text Search: {len(queries)} sorgu, {TEXT_SEARCH_WORKERS} worker")
    tasks = {q: (lambda q=q: get_places_text(q)) for q in queries}
    results, failed = run_parallel(tasks, TEXT_SEARCH_WORKERS, "Text Search")
    for places in results:
        for p in places:
            add(p)

    # Basarisiz sorgulari seri olarak bir tur daha dene
    still_failed = []
    for q in failed:
        try:
            for p in get_places_text(q):
                add(p)
        except Exception as exc:
            print(f"  KALICI BASARISIZ ({q}): {exc}", file=sys.stderr)
            still_failed.append(q)
    msg = f"Text Search bitti: {len(unique)} benzersiz isletme"
    if still_failed:
        msg += f" ({len(still_failed)} sorgu kalici basarisiz)"
    print(msg)

    # --- Yontem 2: Grid + Nearby (sadece Samsun kapsamdaysa) ---
    grid_points = 0
    if GRID_PROVINCE in active_ils:
        grid = build_grid()
        grid_points = len(grid) * 2
        before = len(unique)
        print(f"\n[2/2] Grid + Nearby Search: {len(grid)} nokta x 2 gecis (type=dentist, keyword=diş)")

        tasks = {}
        for lat, lon in grid:
            tasks[f"type@{lat},{lon}"] = (lambda a=lat, b=lon: refine_search(a, b, GRID_RADIUS_M, "type"))
            tasks[f"kw@{lat},{lon}"] = (lambda a=lat, b=lon: refine_search(a, b, GRID_RADIUS_M, "keyword"))
        results, failed = run_parallel(tasks, GRID_WORKERS, "grid")
        for desc_places in results:
            for p in desc_places:
                add(p, require_dental_hint=True)
        for desc in failed:
            mode, coords = desc.split("@")
            lat, lon = map(float, coords.split(","))
            try:
                for p in refine_search(lat, lon, GRID_RADIUS_M, "type" if mode == "type" else "keyword"):
                    add(p, require_dental_hint=True)
            except Exception as exc:
                print(f"  KALICI BASARISIZ ({desc}): {exc}", file=sys.stderr)
        print(f"Grid'den eklenen yeni isletme: {len(unique) - before}")

    print(f"\nToplam benzersiz isletme (ham): {len(unique)}")
    if PAGETOKEN_BROKEN:
        print("Uyari: sayfalama tokeni bu API key'de calismiyor (legacy API kisiti);"
              " sorgu basina en fazla 20 sonuc alinabildi. Grid bolme esigi (20) bunu telafi eder.")
    return list(unique.values()), len(queries) + grid_points


# ---------------------------------------------------------------------------
# Place Details + website taramasi
# ---------------------------------------------------------------------------

def build_rows(places, active_ils):
    print(f"\nPlace Details + website taramasi: {len(places)} isletme, {DETAILS_SCAN_WORKERS} worker")

    def process(place):
        details = get_place_details(place["place_id"])
        website = details.get("website", "")
        address = details.get("formatted_address") or place.get("formatted_address", "") or place.get("vicinity", "")
        il, district = extract_location(address)

        row = {
            "place_id": place["place_id"],
            "name": details.get("name") or place.get("name", ""),
            "il": il,
            "district": district,
            "address": address,
            "website": website,
            "rating": details.get("rating", ""),
            "review_count": details.get("user_ratings_total", ""),
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

    tasks = {p["place_id"]: (lambda p=p: process(p)) for p in places}
    results, failed = run_parallel(tasks, DETAILS_SCAN_WORKERS, "detay taramasi")
    if failed:
        print(f"  {len(failed)} isletmenin detayi alinamadi (bir sonraki taramada tekrar denenir)", file=sys.stderr)

    # Kesin il filtresi: adresi kapsam disi ile cikan kayitlari ele
    rows = [r for r in results if r["il"] in active_ils]
    print(f"Kapsam ici isletme: {len(rows)}")
    return rows


# ---------------------------------------------------------------------------
# Kalici veritabani (places_db.json)
# ---------------------------------------------------------------------------

def load_db():
    if not os.path.exists(PLACES_DB_FILE):
        return {}, True
    with open(PLACES_DB_FILE, encoding="utf-8") as f:
        return json.load(f), False


def merge_db(db, rows, is_first_run):
    """Bu taramanin satirlarini DB ile birlestirir. 'Yeni' = daha once
    taranmis bir ilde ilk kez gorulen isletme (il bootstrap'i haric)."""
    prev_ils = {rec.get("il") for rec in db.values()}
    new_ids = set()

    for row in rows:
        pid = row["place_id"]
        if pid in db:
            first_seen = db[pid].get("first_seen", TODAY)
        else:
            first_seen = TODAY
            if not is_first_run and row["il"] in prev_ils:
                new_ids.add(pid)
        db[pid] = {**row, "first_seen": first_seen, "last_seen": TODAY}

    # Uzun suredir gorulmeyen kayitlari dusur (kapanmis/tasinmis isletmeler)
    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
    stale = [pid for pid, rec in db.items() if rec.get("last_seen", TODAY) < cutoff]
    for pid in stale:
        del db[pid]
    if stale:
        print(f"{len(stale)} eski kayit dusuruldu ({STALE_DAYS} gundur gorulmuyor)")

    with open(PLACES_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=0, sort_keys=True)

    return new_ids


# ---------------------------------------------------------------------------
# Ciktilar: CSV + panel
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "place_id", "name", "il", "district", "address", "website", "rating",
    "review_count", "detected", "server_header", "x_powered_by",
    "status_code", "error", "first_seen", "last_seen", "is_new",
]


def write_csv(records, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def write_dashboard(records, path, template_path, query_count):
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    dashboard_rows = [
        {
            "name": r["name"],
            "address": r["address"],
            "il": r["il"],
            "district": r["district"],
            "website": r["website"],
            "detected": r["detected"],
            "error": r["error"],
            "rating": r["rating"] or None,
            "reviewCount": r["review_count"] or 0,
            "isNew": bool(r.get("is_new")),
        }
        for r in records
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

    active_ils = resolve_scope()
    print(f"Tarama kapsamı: {', '.join(active_ils)} ({len(active_ils)} il)\n")

    start = time.time()

    places, query_count = collect_places(active_ils)
    rows = build_rows(places, active_ils)

    db, is_first_run = load_db()
    new_ids = merge_db(db, rows, is_first_run)
    if is_first_run:
        print(f"\n{PLACES_DB_FILE} ilk kez oluşturuldu; bu taramada 'yeni' etiketi yok (referans temeli).")
    else:
        print(f"\nBu taramada gerçekten yeni tespit edilen işletme: {len(new_ids)}")

    # Panel/CSV her zaman DB'nin TAMAMINI gosterir (dar kapsamli tarama
    # genis taramanin sonuclarini gizlemez)
    records = sorted(db.values(), key=lambda r: (r.get("il") or "", r.get("district") or "", r.get("name") or ""))
    for r in records:
        r["is_new"] = r["place_id"] in new_ids

    write_csv(records, OUTPUT_CSV)
    print(f"CSV yazıldı: {OUTPUT_CSV} ({len(records)} satır)")

    write_dashboard(records, OUTPUT_DASHBOARD, TEMPLATE_FILE, query_count)
    print(f"Panel oluşturuldu: {OUTPUT_DASHBOARD}")

    il_counts = {}
    for r in records:
        il_counts[r["il"]] = il_counts.get(r["il"], 0) + 1
    print("\nİl bazında toplam:")
    for il, c in sorted(il_counts.items(), key=lambda x: -x[1]):
        print(f"  {il}: {c}")

    elapsed = time.time() - start
    print(f"\nToplam süre: {elapsed / 60:.1f} dakika")


if __name__ == "__main__":
    main()
