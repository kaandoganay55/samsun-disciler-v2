# Karadeniz Diş Klinikleri - Web Altyapı Taraması (v2)

Karadeniz bölgesindeki (18 il, varsayılan modda sadece Samsun) diş
klinikleri/diş hekimlerini bulur, web sitelerini tarar ve hangi altyapıyı
(WordPress, özel kod, vb.) kullandıklarını tespit eder.

İşletme keşfi iki yöntemin birleşimiyle yapılır:

- **Text Search**: anahtar kelime × (il + ilçe) sorguları. Samsun için 20
  anahtar kelimelik geniş liste, diğer iller için 7 kelimelik çekirdek liste
  (maliyet kontrolü).
- **Grid + Nearby Search** (sadece Samsun): il sabit aralıklı koordinat
  noktalarına bölünüp her noktada iki geçiş yapılır (`type=dentist` +
  `keyword=diş`). Yoğun bölgelerde (60 sonuç tavanına çarpılırsa) nokta
  otomatik olarak küçük yarıçaplı alt noktalara bölünür (quad-tree).

Sonuçlar `place_id`'ye göre tekilleştirilir. Tüm API çağrıları kota/ağ
hatalarında üstel bekleme ile yeniden denenir; kalıcı başarısız sorgular
çalışma sonunda raporlanır (sessiz veri kaybı olmaz).

## Kalıcı veritabanı (`places_db.json`)

Görülen her işletme `first_seen`/`last_seen` tarihleriyle saklanır. Dar
kapsamlı tarama (haftalık Samsun) geniş taramanın (aylık Karadeniz)
sonuçlarını **silmez**, sadece kendi kapsamını tazeler. Panel her zaman
veritabanının tamamını gösterir. 120 gündür görülmeyen kayıtlar düşürülür.

## Kurulum

```
pip3 install -r requirements.txt
```

Bir Google Places API key al (console.cloud.google.com → proje oluştur →
"Places API"yi etkinleştir → Credentials → Create API key), sadece
"Places API" ile kısıtla.

## Çalıştırma

```
export GOOGLE_PLACES_API_KEY="senin-keyin"

SCAN_SCOPE=samsun     python3 api_versiyon.py   # varsayılan: sadece Samsun
SCAN_SCOPE=karadeniz  python3 api_versiyon.py   # 18 ilin tamamı
SCAN_SCOPE="Samsun,Ordu,Trabzon" python3 api_versiyon.py  # seçili iller
```

## Çıktılar

- `samsun_disciler_altyapi.csv` — ham veri (il/ilçe, website, altyapı,
  puan/yorum, first_seen/last_seen, is_new)
- `places_db.json` — kalıcı işletme veritabanı
- `index.html` — görsel panel (özet sayılar, il/ilçe dağılımı, il + ilçe +
  durum filtreleri, arama). Script her çalıştığında yeniden üretilir.

## Otomatik tarama (GitHub Actions)

- **Haftalık** (Pazartesi 08:00 TR): sadece Samsun — ucuz tazeleme
- **Aylık** (ayın 1'i): tüm Karadeniz — tam tarama
- **Manuel**: Actions → "Diş Klinikleri Taraması" → Run workflow → kapsam seç
- **Push**: `api_versiyon.py` / panel / workflow değişince otomatik Samsun
  taraması; commit mesajında `[tam]` geçerse Karadeniz taraması

Kurulum: `GOOGLE_PLACES_API_KEY` secret'ı + GitHub Pages (main / root).
Panel: `https://KULLANICI_ADIN.github.io/REPO_ADIN/`

**Maliyet notu:** Samsun taraması ~600 arama + ~400 detay çağrısı; tam
Karadeniz taraması ~2000 arama + ~2000 detay çağrısı yapar. Google'ın
ücretsiz aylık kotaları çoğunu karşılar ama tam taramayı haftalıktan sık
çalıştırmadan önce faturayı takip et (console.cloud.google.com → Billing).
