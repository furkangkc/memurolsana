import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# --- AYARLAR ---
BASE_URL = "https://kamuilan.sbb.gov.tr/"
SITE_URL = os.environ.get("SITE_URL", "https://siteadiniz.com").rstrip("/")
DATA_FILE = "data/ilanlar.json"
ILAN_DIR = "ilanlar"
SITEMAP_FILE = "sitemap.xml"

MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

API_BEKLEME_SURESI = 12
SITE_BEKLEME_SURESI = 1.5
MAX_YENI_ILAN = 15
MAX_PDF_BOYUTU = 15 * 1024 * 1024

HEADERS = {
    "User-Agent": "KamuAlimBilgiBotu/1.0 (+https://siteadiniz.com)"
}

ETIKETLER = {
    "ortaogretim": "Ortaöğretim (Lise)",
    "onlisans": "Önlisans",
    "lisans": "Lisans",
}


class KadroKalemi(BaseModel):
    unvan: str = Field(description="Kadro veya pozisyon unvanı")
    ogrenim: Literal["ortaogretim", "onlisans", "lisans"] = Field(description="İstenen öğrenim düzeyi")
    kontenjan: int = Field(description="Alınacak kişi sayısı, belirtilmemişse 0")
    kpss_turu: str = Field(description="İstenen KPSS puan türü ve taban puanı")


class IlanSemasi(BaseModel):
    kurum: str = Field(description="İlanı yayınlayan resmi kamu kurumu adı")
    baslik: str = Field(description="Sade, anlaşılır ve dikkat çekici ilan başlığı")
    kategoriler: list[Literal["ortaogretim", "onlisans", "lisans"]] = Field(description="Alım yapılan öğrenim düzeyleri")
    kpss: Literal["kpssli", "sartsiz", "belirsiz"] = Field(description="KPSS puan şartı durumu")
    kpss_detay: str = Field(description="Kısa KPSS açıklaması")
    sehir: str = Field(description="Alım yapılacak il veya 'Türkiye Geneli'")
    toplam_kontenjan: int = Field(description="Toplam alınacak personel sayısı, belirsizse 0")
    baslangic_tarihi: str = Field(description="Başvuru başlangıç tarihi GG.AA.YYYY, yoksa boş")
    son_basvuru_tarihi: str = Field(description="Son başvuru tarihi GG.AA.YYYY, yoksa boş")
    ozet: str = Field(description="Adaylar için 2-3 cümlelik net ve özgün özet")
    kadrolar: list[KadroKalemi] = Field(description="Başlıca kadro dağılımı (en fazla 15 satır)")
    sartlar: list[str] = Field(description="En önemli 4-6 başvuru şartı")
    basvuru_sekli: str = Field(description="Başvurunun nereden ve nasıl yapılacağı")


PROMPT = """
Aşağıda bir Türkiye Cumhuriyeti kamu personel alım ilanının web sayfası metni ve varsa ekli PDF kılavuzu yer almaktadır.
Bu ilanı analiz edip istenen JSON şemasına göre eksiksiz doldur.

Kurallar:
- Öğrenim düzeyleri: Lise/Ortaöğretim -> "ortaogretim", Önlisans/MYO -> "onlisans", Lisans/Fakülte -> "lisans".
- Sadece "önlisans" geçen bir ilanı yanlışlıkla "lisans" olarak işaretleme.
- Özet ve şartları adayların kolayca anlayacağı şekilde sade ve özgün ifadelerle yaz.
- Metinde geçmeyen bilgi uydurma; tarih yoksa boş string, sayı yoksa 0 yaz.
"""


def slugify(metin: str, ilan_id: str) -> str:
    ceviri = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    temiz = metin.translate(ceviri).lower()
    temiz = re.sub(r"[^a-z0-9]+", "-", temiz).strip("-")
    return f"{temiz[:65]}-{ilan_id[:6]}"


def mevcut_ilanlari_yukle() -> list[dict]:
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def ilanlari_kaydet(ilanlar: list[dict]) -> None:
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(ilanlar[:200], f, ensure_ascii=False, indent=2)


def seo_sayfasi_olustur(ilan: dict) -> None:
    """Her ilan için Google AdSense ve SEO uyumlu ayrı statik HTML sayfası üretir."""
    os.makedirs(ILAN_DIR, exist_ok=True)
    slug = ilan["slug"]
    e = html.escape

    kat_rozet = " ".join(
        f'<span class="badge">{e(ETIKETLER.get(k, k))}</span>'
        for k in ilan.get("kategoriler", [])
    )

    kadro_satirlari = "".join(
        f"<tr><td>{e(str(k.get('unvan', '')))}</td>"
        f"<td>{e(ETIKETLER.get(k.get('ogrenim', ''), ''))}</td>"
        f"<td>{k.get('kontenjan', 0) or '-'}</td>"
        f"<td>{e(str(k.get('kpss_turu', '-')))}</td></tr>"
        for k in ilan.get("kadrolar", [])
    )
    kadro_html = (
        f"<table><thead><tr><th>Unvan</th><th>Öğrenim</th><th>Kontenjan</th><th>KPSS</th></tr></thead>"
        f"<tbody>{kadro_satirlari}</tbody></table>"
        if kadro_satirlari
        else "<p>Detaylı kadro listesi için resmi ilan metnini inceleyiniz.</p>"
    )

    sartlar_html = "".join(f"<li>{e(s)}</li>" for s in ilan.get("sartlar", []))
    pdf_btn = (
        f'<a class="btn outline" href="{e(ilan["pdf_url"])}" target="_blank" rel="noopener noreferrer">PDF Kılavuzu</a>'
        if ilan.get("pdf_url")
        else ""
    )

    sayfa_html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{e(ilan.get("baslik", ""))} | KamuAlım</title>
  <meta name="description" content="{e(ilan.get("ozet", "")[:155])}" />
  <style>
    :root {{ --bg:#f8fafc; --card:#fff; --text:#0f172a; --muted:#475569; --primary:#1d4ed8; --border:#e2e8f0; }}
    * {{ box-sizing:border-box; margin:0; padding:0; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); line-height:1.65; }}
    header {{ background:var(--card); border-bottom:1px solid var(--border); padding:15px 20px; }}
    .nav {{ max-width:860px; margin:0 auto; display:flex; justify-content:space-between; align-items:center; }}
    .logo {{ font-weight:800; font-size:1.2rem; color:var(--primary); text-decoration:none; }}
    main {{ max-width:860px; margin:28px auto; padding:0 20px; }}
    .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:28px; }}
    .kurum {{ color:var(--primary); font-weight:700; font-size:0.88rem; text-transform:uppercase; }}
    h1 {{ font-size:1.65rem; margin:6px 0 12px; line-height:1.3; }}
    .badges {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }}
    .badge {{ background:#eff6ff; color:#1e40af; padding:4px 10px; border-radius:6px; font-size:0.82rem; font-weight:700; }}
    .meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; background:#f8fafc; padding:14px; border-radius:10px; margin:16px 0; font-size:0.9rem; }}
    h2 {{ font-size:1.12rem; color:var(--primary); margin:22px 0 10px; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.9rem; }}
    th, td {{ text-align:left; padding:10px; border-bottom:1px solid var(--border); }}
    th {{ background:#f8fafc; }}
    ul {{ padding-left:20px; color:var(--muted); }}
    li {{ margin-bottom:6px; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:24px; }}
    .btn {{ display:inline-block; padding:11px 18px; border-radius:10px; font-weight:700; text-decoration:none; background:var(--primary); color:#fff; }}
    .btn.outline {{ background:#fff; color:var(--text); border:1px solid var(--border); }}
    footer {{ max-width:860px; margin:32px auto; padding:20px; text-align:center; font-size:0.85rem; color:var(--muted); border-top:1px solid var(--border); }}
    footer a {{ color:var(--muted); text-decoration:none; margin:0 8px; }}
  </style>
</head>
<body>
  <header>
    <div class="nav">
      <a href="../index.html" class="logo">KamuAlım</a>
      <a href="../index.html" style="text-decoration:none;color:#475569;font-weight:600;">← Tüm İlanlar</a>
    </div>
  </header>
  <main>
    <article class="card">
      <div class="kurum">{e(ilan.get("kurum", ""))}</div>
      <h1>{e(ilan.get("baslik", ""))}</h1>
      <div class="badges">{kat_rozet}</div>
      <p style="color:#334155;">{e(ilan.get("ozet", ""))}</p>

      <div class="meta">
        <div><strong>Şehir:</strong> {e(ilan.get("sehir", "-"))}</div>
        <div><strong>Kontenjan:</strong> {ilan.get("toplam_kontenjan", 0) or "Belirtilmemiş"}</div>
        <div><strong>KPSS:</strong> {e(ilan.get("kpss_detay", "-"))}</div>
        <div><strong>Son Başvuru:</strong> {e(ilan.get("son_basvuru_tarihi", "-") or "İlan metnine bakınız")}</div>
      </div>

      <h2>Kadro ve Kontenjan Dağılımı</h2>
      {kadro_html}

      <h2>Başvuru Şartları</h2>
      <ul>{sartlar_html}</ul>

      <h2>Başvuru Şekli</h2>
      <p style="color:#334155;">{e(ilan.get("basvuru_sekli", ""))}</p>

      <div class="actions">
        <a class="btn" href="{e(ilan.get("kaynak_url", BASE_URL))}" target="_blank" rel="noopener noreferrer">Resmi İlan Sayfasına Git ↗</a>
        {pdf_btn}
      </div>
    </article>
  </main>
  <footer>
    <p>© 2026 KamuAlım — Bağımsız bilgilendirme platformudur.</p>
    <p style="margin-top:8px;">
      <a href="../hakkimizda.html">Hakkımızda</a>
      <a href="../iletisim.html">İletişim</a>
      <a href="../gizlilik-politikasi.html">Gizlilik Politikası</a>
      <a href="../sorumluluk-reddi.html">Sorumluluk Reddi</a>
    </p>
  </footer>
</body>
</html>
"""
    with open(os.path.join(ILAN_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
        f.write(sayfa_html)


def sitemap_guncelle(ilanlar: list[dict]) -> None:
    bugun = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sabit_sayfalar = [
        "",
        "hakkimizda.html",
        "iletisim.html",
        "gizlilik-politikasi.html",
        "sorumluluk-reddi.html",
    ]
    urller = [f"  <url><loc>{SITE_URL}/{s}</loc><lastmod>{bugun}</lastmod></url>" for s in sabit_sayfalar]
    for ilan in ilanlar:
        if ilan.get("slug"):
            urller.append(
                f"  <url><loc>{SITE_URL}/ilanlar/{ilan['slug']}.html</loc><lastmod>{bugun}</lastmod></url>"
            )

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "\n".join(urller)
    xml += "\n</urlset>\n"
    with open(SITEMAP_FILE, "w", encoding="utf-8") as f:
        f.write(xml)


def sayfa_ve_pdf_indir(url: str) -> tuple[str, str | None, bytes | None]:
    resp = requests.get(url, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    sayfa_metni = soup.get_text("\n", strip=True)[:15000]

    pdf_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() or "dosya" in href.lower() or "indir" in href.lower():
            pdf_url = urljoin(BASE_URL, href)
            break

    pdf_bytes = None
    if pdf_url:
        try:
            pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=30)
            pdf_resp.raise_for_status()
            ctype = pdf_resp.headers.get("Content-Type", "").lower()
            if ("pdf" in ctype or pdf_resp.content[:4] == b"%PDF") and len(pdf_resp.content) <= MAX_PDF_BOYUTU:
                pdf_bytes = pdf_resp.content
        except Exception as e:
            print(f"  PDF indirilemedi: {e}")

    return sayfa_metni, pdf_url, pdf_bytes


def gemini_ile_ayristir(client: genai.Client, baslik: str, sayfa_metni: str, pdf_bytes: bytes | None) -> dict:
    icerikler: list = [
        f"{PROMPT}\n\n--- İLAN BAŞLIĞI ---\n{baslik}\n\n--- WEB SAYFASI METNİ ---\n{sayfa_metni}"
    ]
    if pdf_bytes:
        icerikler.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))

    bekleme = [30, 60, 90]
    for deneme in range(len(bekleme) + 1):
        try:
            yanit = client.models.generate_content(
                model=MODEL_NAME,
                contents=icerikler,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=IlanSemasi,
                    temperature=0.1,
                ),
            )
            return json.loads(yanit.text)
        except Exception as e:
            if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) and deneme < len(bekleme):
                print(f"  Rate limit (429). {bekleme[deneme]} sn bekleniyor...")
                time.sleep(bekleme[deneme])
            else:
                raise


def calistir():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY ortam değişkeni tanımlı değil!")

    client = genai.Client(api_key=api_key)
    mevcut_ilanlar = [i for i in mevcut_ilanlari_yukle() if not str(i.get("id", "")).startswith("ornek-")]
    mevcut_idler = {item["id"] for item in mevcut_ilanlar}

    resp = requests.get(BASE_URL, headers=HEADERS, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    islenen = 0
    for a in soup.find_all("a", href=True):
        if islenen >= MAX_YENI_ILAN:
            break
        href = a["href"]
        if "ilandetay.aspx" not in href.lower():
            continue

        tam_url = urljoin(BASE_URL, href)
        ilan_id = hashlib.md5(tam_url.encode("utf-8")).hexdigest()[:12]
        if ilan_id in mevcut_idler:
            continue

        ham_baslik = a.get_text(" ", strip=True)
        if not ham_baslik or len(ham_baslik) < 5:
            continue

        print(f"İnceleniyor: {ham_baslik}")
        try:
            time.sleep(SITE_BEKLEME_SURESI)
            sayfa_metni, pdf_url, pdf_bytes = sayfa_ve_pdf_indir(tam_url)
            ayristirilmis = gemini_ile_ayristir(client, ham_baslik, sayfa_metni, pdf_bytes)

            yeni_kayit = {
                "id": ilan_id,
                "slug": slugify(ayristirilmis.get("baslik", ham_baslik), ilan_id),
                "kaynak_url": tam_url,
                "pdf_url": pdf_url,
                "eklenme_tarihi": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                **ayristirilmis,
            }

            mevcut_ilanlar.insert(0, yeni_kayit)
            mevcut_idler.add(ilan_id)
            seo_sayfasi_olustur(yeni_kayit)
            ilanlari_kaydet(mevcut_ilanlar)
            sitemap_guncelle(mevcut_ilanlar)

            islenen += 1
            print(f"  Kaydedildi ({islenen}/{MAX_YENI_ILAN})")
            time.sleep(API_BEKLEME_SURESI)
        except Exception as e:
            print(f"  Hata ({tam_url}): {e}")
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                break

    print(f"Tamamlandı. {islenen} yeni ilan eklendi.")


if __name__ == "__main__":
    calistir()
