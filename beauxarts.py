"""
Lit « L'Atlas » des Beaux-Arts de Paris (prix et bourses des étudiants et jeunes diplômés)
et écrit data/beauxarts.json pour l'app Trois.

Lancé chaque nuit par GitHub Actions (.github/workflows/beauxarts.yml).
Seules les pages d'œuvres nouvelles sont téléchargées : les autres sont reprises du JSON existant.
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://beauxartsparis.fr"
ATLAS = BASE + "/fr/latlas"
OUT = os.path.join("data", "beauxarts.json")
KEEP_GALLERY = re.compile(r"prix|lauréat|laureat|bourse", re.I)   # galeries gardées : une page d'œuvre = un artiste
IMG_RE = re.compile(r"/sites/default/files/.+\.(jpe?g|png|gif|webp)(\?.*)?$", re.I)
HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
TILE_HEADINGS = ["h2", "h3", "h4", "h5", "h6"]

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (compatible; trois-app/1.0; lecture quotidienne de L'Atlas)"


def get_soup(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    time.sleep(1)  # politesse envers le site de l'école
    return BeautifulSoup(r.text, "html.parser")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def meta(soup, prop):
    t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return clean(t.get("content")) if t and t.get("content") else ""


def abs_img(src):
    if not src:
        return None
    u = urljoin(BASE, src.strip())
    return u if IMG_RE.search(urlparse(u).path) and "header" not in u.lower() else None


def page_title(soup):
    t = meta(soup, "og:title")
    if not t and soup.title:
        t = soup.title.get_text().split("|")[0]
    return clean(t)


def gallery_links(soup):
    """Tuiles de l'Atlas : liens internes qui entourent une image du site."""
    out = []
    for a in soup.find_all("a", href=True):
        img = a.find("img")
        if not img or "/sites/default/files/" not in (img.get("src") or ""):
            continue
        href = urljoin(BASE, a["href"].strip())
        if urlparse(href).netloc != urlparse(BASE).netloc or "/Oeuvre/" in href:
            continue
        if href not in out:
            out.append(href)
    return out


def tiles(soup):
    """Dans une galerie : pour chaque page d'œuvre, nom, prix, légende et image de la tuile."""
    found = {}
    for a in soup.select('a[href*="/Oeuvre/"]'):
        href = urljoin(BASE, a["href"].strip())
        t = found.setdefault(href, {"url": href, "name": "", "prize": "", "caption": "", "img": None})
        img = a.find("img")
        if img and not t["img"]:
            t["img"] = abs_img(img.get("src"))
        # remonter jusqu'au bloc de la tuile (qui ne contient qu'une seule page d'œuvre)
        node, block = a, None
        for _ in range(6):
            parent = node.parent
            if parent is None:
                break
            hrefs = {urljoin(BASE, x["href"].strip()) for x in parent.select('a[href*="/Oeuvre/"]')}
            if len(hrefs) > 1:
                break
            node = parent
            if block is None and node.find(TILE_HEADINGS):
                block = node   # le plus petit bloc qui contient la tuile et ses titres
        if block is not None and not t["name"]:
            heads = [clean(h.get_text()) for h in block.find_all(TILE_HEADINGS) if clean(h.get_text())]
            if heads:
                t["name"] = heads[0]
                t["prize"] = heads[1] if len(heads) > 1 else ""
            text = clean(block.get_text(" "))
            for h in heads:
                text = text.replace(h, " ")
            text = clean(text.replace("Loading...", ""))
            t["caption"] = text[:220]
    return list(found.values())


def oeuvre(url, tile):
    """Page d'œuvre : bio en français, images."""
    soup = get_soup(url)
    title = page_title(soup)
    name = tile["name"] or clean(title.split(" - ")[0])
    paras = []
    start = None
    for h in soup.find_all(HEADINGS):
        if norm(h.get_text()) == norm(name):
            start = h
            break
    if start is not None:
        for el in start.find_all_next():
            if el.name == "hr":
                break
            if el.name == "p":
                txt = clean(el.get_text(" "))
                if not txt:
                    continue
                em = el.find(["em", "i"])
                if em and clean(em.get_text(" ")) == txt:   # paragraphe entièrement en italique = traduction anglaise
                    break
                if re.match(r"^(Born|Lives|A graduate)\b", txt):
                    break
                paras.append(txt)
                if len(paras) >= 6:
                    break
    bio = "\n".join(paras)
    if len(bio) < 80:
        bio = meta(soup, "og:description") or meta(soup, "description")
    imgs = []
    for tag in soup.find_all(["a", "img"]):
        u = abs_img(tag.get("href") if tag.name == "a" else tag.get("src"))
        if u and u not in imgs:
            imgs.append(u)
    m = re.search(r"\b(Née?)\s+(?:en\s+)?(\d{4})", bio)
    return {
        "name": name,
        "bio": bio[:1500],
        "images": imgs[:6],
        "born": int(m.group(2)) if m else None,
        "bornWord": m.group(1) if m else None,
    }


def main():
    old = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        for a in prev.get("artists", []):
            for w in a.get("pages", []):
                old[w] = a
    try:
        atlas = get_soup(ATLAS)
    except Exception as e:
        print("L'Atlas ne répond pas, données inchangées :", e)
        return 0

    artists = {}
    galleries = 0
    for g in gallery_links(atlas):
        try:
            gs = get_soup(g)
        except Exception as e:
            print("Galerie ignorée", g, e)
            continue
        gtitle = page_title(gs)
        if not KEEP_GALLERY.search(gtitle):
            continue
        galleries += 1
        ym = re.search(r"(20\d\d)", gtitle) or re.search(r"(20\d\d)", g)
        year = int(ym.group(1)) if ym else None
        for t in tiles(gs):
            key = None
            if t["url"] in old and old[t["url"]].get("bio"):
                rec = dict(old[t["url"]])
            else:
                try:
                    info = oeuvre(t["url"], t)
                except Exception as e:
                    print("Œuvre ignorée", t["url"], e)
                    continue
                works = []
                seen = set()
                for u in ([t["img"]] if t["img"] else []) + info["images"]:
                    f = os.path.basename(urlparse(u).path).lower()
                    if f in seen:
                        continue
                    seen.add(f)
                    works.append({"img": u, "cap": t["caption"] if u == t["img"] or not works else ""})
                rec = {
                    "id": urlparse(t["url"]).path.rsplit("/", 1)[-1],
                    "name": info["name"],
                    "prize": t["prize"],
                    "collection": gtitle,
                    "year": year,
                    "born": info["born"],
                    "bornWord": info["bornWord"],
                    "bio": info["bio"],
                    "works": works,
                    "url": t["url"],
                    "pages": [t["url"]],
                }
            if not rec.get("name") or not rec.get("works"):
                continue
            key = norm(rec["name"])
            if key in artists:   # même artiste dans plusieurs galeries : on fusionne
                a = artists[key]
                have = {os.path.basename(urlparse(w["img"]).path).lower() for w in a["works"]}
                a["works"] += [w for w in rec["works"] if os.path.basename(urlparse(w["img"]).path).lower() not in have]
                a["pages"] = sorted(set(a["pages"] + rec["pages"]))
                if (rec.get("year") or 0) > (a.get("year") or 0):
                    for k in ("prize", "collection", "year", "url"):
                        a[k] = rec[k]
                if len(rec.get("bio", "")) > len(a.get("bio", "")):
                    a["bio"] = rec["bio"]
            else:
                artists[key] = rec

    result = sorted(artists.values(), key=lambda a: (-(a.get("year") or 0), a["name"]))
    print(f"{galleries} galeries de prix, {len(result)} artistes")
    if not result:
        print("Aucun artiste trouvé : le site a peut-être changé. Données inchangées.")
        return 1

    prev_artists = None
    checked = None
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
        prev_artists, checked = prev.get("artists"), prev.get("checked")
    now = datetime.now(timezone.utc)
    # Date de vérification réécrite au plus une fois par semaine : garde le robot actif
    # (GitHub met en pause les tâches planifiées d'un dépôt sans activité pendant 60 jours).
    stale = not checked or (now - datetime.fromisoformat(checked)).days >= 7
    if prev_artists == result and not stale:
        print("Rien de neuf.")
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"source": ATLAS, "checked": now.isoformat(timespec="seconds"), "artists": result},
                  f, ensure_ascii=False, indent=1)
    print("Fichier écrit :", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
