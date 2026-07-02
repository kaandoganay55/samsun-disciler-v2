# Samsun Diş Klinikleri - Web Altyapı Taraması (v2)

Samsun'daki diş klinikleri/diş hekimlerini bulur, web sitelerini tarar ve
hangi altyapıyı (WordPress, özel kod, vb.) kullandıklarını tespit eder.

İşletme keşfi iki yöntemin birleşimiyle yapılır:

- **Text Search**: anahtar kelime + ilçe metniyle arama (17 anahtar kelime ×
  17 ilçe + genel sorgular)
- **Grid + Nearby Search**: Samsun'u sabit aralıklı koordinat noktalarına
  bölüp her noktada `type=dentist` ile arama. Yoğun bölgelerde (60 sonuç
  tavanına çarpılırsa) o nokta otomatik olarak küçük yarıçaplı alt
  noktalara bölünür.

İki yöntemin sonucu `place_id`'ye göre tekilleştirilip birleştirilir. Tüm
ağır işlemler (Text Search sorguları, grid noktaları, Place Details +
website taraması) paralel çalışır.

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
python3 api_versiyon.py
```

## Çıktılar

- `samsun_disciler_altyapi.csv` — ham veri
- `index.html` — tarayıcıda çift tıklayarak açılan görsel panel (özet
  sayılar, genel + ilçe bazlı dağılım grafiği, filtrelenebilir/aranabilir
  liste). Script her çalıştığında bu dosya güncel veriyle yeniden üretilir.

## Deploy — otomatik tarama + canlı yayın (GitHub Pages + Actions)

1. Bu klasörü GitHub'daki repoya push et.
2. **API key'i secret olarak ekle:** Repo → Settings → Secrets and
   variables → Actions → New repository secret
   - Name: `GOOGLE_PLACES_API_KEY`
   - Value: senin key'in
3. **GitHub Pages'i aç:** Repo → Settings → Pages → Source: "Deploy from a
   branch" → Branch: `main`, klasör: `/ (root)` → Save.
4. **İlk taramayı manuel tetikle:** Repo → Actions → "Samsun Diş Klinikleri
   Taraması" → Run workflow.
5. Panel şurada yayında olur: `https://KULLANICI_ADIN.github.io/REPO_ADIN/`

**Not:** Her çalıştırma ~400+ arama sorgusu + ~400 site taraması yapıyor,
Google'ın aylık 200$'lık ücretsiz kotasının bir kısmını tüketiyor. Haftalık
tarama fazlasıyla yeterli — günlük çalıştırmaya çevirmeden önce kotayı takip
et (console.cloud.google.com → APIs & Services → Quotas).
