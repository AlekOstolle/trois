"""
Robot « contemporains » de l'app Trois.

Chaque nuit (GitHub Actions), il parcourt des sources d'art contemporain et enregistre
des fiches d'artistes (nom, année de naissance, bio, images d'œuvres) dans data/contemporains/.

Sources :
  - galeries      : toutes les galeries « contemporain » de l'annuaire du Comité professionnel
                    des galeries d'art (CPGA). Pour chaque galerie, le robot trouve son site,
                    sa page « Artistes », puis les fiches de ses artistes.
  - beauxarts     : L'Atlas des Beaux-Arts de Paris (prix et bourses des étudiants et jeunes diplômés).
  - dda           : Réseau documents d'artistes (plus de 800 artistes en régions).
  - prix          : prix et fondations qui repèrent de jeunes artistes (liste SOURCES ci-dessous).

Il respecte robots.txt, attend 1 seconde entre deux pages d'un même site, et ne lit qu'un
nombre limité de nouvelles fiches par nuit : la base grossit un peu chaque nuit.
Ajouter une source = ajouter une ligne dans SOURCES.
"""
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ RÉGLAGES
MIN_BORN = 1955              # artistes nés avant cette année : ignorés (quand l'année est connue)
MAX_NEW_PAGES = 180          # nouvelles fiches lues par nuit, toutes sources confondues
MAX_NEW_PER_SOURCE = 8       # par source et par nuit (pour varier)
GALLERIES_PER_NIGHT = 80     # galeries du CPGA explorées par nuit (les autres les nuits suivantes)
INDEX_REFRESH_DAYS = 7       # relecture des listes d'artistes
GALLERY_REFRESH_DAYS = 30    # relecture des fiches galerie du CPGA
RETRY_REJECTED_DAYS = 90
TIME_BUDGET_S = 40 * 60

# type "list" : une page qui liste les artistes (index) + motif des adresses de fiches (regex sur le chemin).
# Le robot lit aussi le plan du site (sitemap) pour trouver les fiches que l'index ne montre pas.
SOURCES = [
    {"key": "atlas", "name": "Beaux-Arts de Paris", "family": "beauxarts", "type": "atlas",
     "index": "https://beauxartsparis.fr/fr/latlas"},
    {"key": "dda", "name": "Réseau documents d'artistes", "family": "dda", "type": "list",
     "index": "https://reseau-dda.org/fr/artists", "pattern": r"^/fr/artists/[a-z0-9-]+/?$"},
    {"key": "prix-jfp", "name": "Prix Jean-François Prat", "family": "prix", "type": "list",
     "index": "https://prixjeanfrancoisprat.com/", "pattern": r"^/artiste/[^/]+/?$"},
    {"key": "bredin-prat", "name": "Fonds de dotation Bredin Prat", "family": "prix", "type": "list",
     "index": "https://www.bredinpratfoundation.org/", "pattern": r"^/artiste/[^/]+/?$"},
    {"key": "frances", "name": "Fondation Francès", "family": "prix", "type": "list",
     "index": "https://www.fondationfrances.com/artistes/", "pattern": r"^/artistes/[^/]+/?$"},
    {"key": "artmemo", "name": "Françoise art memo", "family": "prix", "type": "list",
     "index": "https://francoiseartmemo.fr/", "pattern": r"^/artiste/[^/]+/?$"},
    {"key": "cpga", "name": "Galeries du CPGA", "family": "galeries", "type": "cpga",
     "index": "https://www.comitedesgaleriesdart.com/galeries/?specialites=contemporain"},
]

DATA = os.path.join("data", "contemporains")
ADIR = os.path.join(DATA, "a")
INDEX = os.path.join(DATA, "index.json")
STATE = os.path.join("data", "robot-state.json")

UA_TOKEN = "TroisBot"
UA = f"Mozilla/5.0 (compatible; {UA_TOKEN}/1.0; +https://github.com/{os.environ.get('GITHUB_REPOSITORY', '')})"
session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"})

START = time.time()
NOW = time.time()
_last_hit = {}
_robots = {}
stats = {}


def log(*a):
    print(*a, flush=True)


def out_of_time():
    return time.time() - START > TIME_BUDGET_S


# ------------------------------------------------------------------ outils texte
def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def slugify(s, n=90):
    return re.sub(r"-+", "-", norm(s).replace(" ", "-")).strip("-")[:n] or "x"


def meta(soup, prop):
    t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return clean(t.get("content")) if t and t.get("content") else ""


def root_of(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def same_site(a, b):
    return urlparse(a).netloc.lower().removeprefix("www.") == urlparse(b).netloc.lower().removeprefix("www.")


def dedupe(urls):
    seen, out = set(), []
    for u in urls:
        k = urlparse(u)._replace(query="", fragment="").geturl().rstrip("/").lower()
        if k not in seen:
            seen.add(k)
            out.append(u.split("#")[0])
    return out


# ------------------------------------------------------------------ réseau poli
def allowed(url):
    base = root_of(url)
    if base not in _robots:
        rp = None
        try:
            r = session.get(base + "/robots.txt", timeout=15)
            if r.status_code == 200:
                rp = robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(UA_TOKEN, url)


def fetch(url, xml=False):
    if out_of_time() or not url.startswith("http"):
        return None
    if not allowed(url):
        log("  robots.txt interdit :", url)
        return None
    dom = urlparse(url).netloc
    wait = 1.0 - (time.time() - _last_hit.get(dom, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[dom] = time.time()
    try:
        r = session.get(url, timeout=25)
    except Exception as e:
        log("  erreur réseau :", url, type(e).__name__)
        return None
    if r.status_code != 200:
        return None
    if not xml and "html" not in (r.headers.get("content-type") or "html").lower():
        return None
    return r


def soup_of(url):
    r = fetch(url)
    if not r:
        return None, url
    return BeautifulSoup(r.text, "html.parser"), (getattr(r, "url", None) or url)


def links(soup, base):
    out = []
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if not h or h.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        out.append((urljoin(base, h).split("#")[0], clean(a.get_text(" "))))
    return out


def sitemap_urls(base, pattern, limit=4000):
    """Adresses du plan du site (sitemap) dont le chemin correspond au motif."""
    root = root_of(base)
    allowed(root + "/")
    rp = _robots.get(root)
    cands = list((rp.site_maps() if rp else None) or [])
    cands += [root + "/sitemap.xml", root + "/sitemap_index.xml", root + "/wp-sitemap.xml"]
    pat = re.compile(pattern)
    queue, seen, found = [], set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            queue.append(c)
    n = 0
    while queue and n < 25 and len(found) < limit:
        sm = queue.pop(0)
        n += 1
        r = fetch(sm, xml=True)
        if not r:
            continue
        try:
            doc = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        kind = doc.tag.split("}")[-1]
        subs = []
        for el in doc:
            loc = next((clean(c.text) for c in el if c.tag.split("}")[-1] == "loc" and c.text), None)
            if not loc:
                continue
            if kind == "sitemapindex":
                if loc not in seen:
                    seen.add(loc)
                    subs.append(loc)
            elif pat.search(urlparse(loc).path):
                found.append(loc)
        # d'abord les sous-plans qui parlent d'artistes
        subs.sort(key=lambda u: 0 if re.search(r"artist", u, re.I) else 1)
        queue = subs + queue
    return dedupe(found)


# ------------------------------------------------------------------ extraction générique d'une fiche
BAD_IMG = re.compile(r"logo|icon|sprite|avatar|placeholder|blank|spacer|loader|favicon|social|facebook|"
                     r"instagram|twitter|pixel|1x1|arrow|button|badge|flag|drapeau|newsletter", re.I)
IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)
BOILER = re.compile(r"cookie|newsletter|©|copyright|tous droits|all rights|javascript|abonnez|inscrivez|"
                    r"subscribe|politique de confidentialit|privacy", re.I)
GENERIC_NAMES = re.compile(r"^(artistes?|artists?|accueil|home|expositions?|exhibitions?|overview|"
                           r"biographie|biography|contact|news|actualit[ée]s?|galerie|gallery|works?|œuvres?)$", re.I)
BORN_FR = re.compile(r"\b(N[ée](?:[⋅·.\-]?e)?)\b[^.\n]{0,60}?\b(19[2-9]\d|200\d)\b")
BORN_EN = re.compile(r"\b(?:[Bb]orn|b\.)\s[^.\n]{0,50}?\b(19[2-9]\d|200\d)\b")
BORN_PAREN = re.compile(r"\((?:[A-Z]{2,3}|[A-Za-zÀ-ÿ .'-]{3,25}),\s*(19[2-9]\d|200\d)\s*\)")
LIFE = re.compile(r"\((1[89]\d\d|200\d)\s*[-–—]\s*((?:19|20)\d\d)?\s*\)")
BA_RE = re.compile(r"Beaux[- ]Arts de Paris|ENSBA|[ÉE]cole nationale sup[ée]rieure des beaux[- ]arts", re.I)
TITLE_SEP = re.compile(r"\s+[|–—-]\s+|\s+:\s+")


def best_srcset(v):
    if not v:
        return None
    best, bw = None, -1
    for part in v.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1 and bits[1][:-1].isdigit():
            w = int(bits[1][:-1])
        if w >= bw:
            best, bw = bits[0], w
    return best


def name_from(soup):
    cands = []
    t = meta(soup, "og:title") or (soup.title.get_text() if soup.title else "")
    if t:
        cands.append(TITLE_SEP.split(clean(t))[0])
    h1 = soup.find("h1")
    if h1:
        cands.append(clean(h1.get_text(" ")))
    for c in cands:
        c = re.sub(r"\s*\((?:[^)]*)\)\s*$", "", c).strip()
        words = c.split()
        if 1 < len(c) <= 60 and 1 <= len(words) <= 7 and re.search(r"[A-Za-zÀ-ÿ]", c) \
                and not re.search(r"https?:|\d{3,}", c) and not GENERIC_NAMES.match(c):
            return c
    return None


def born_of(text):
    """(année, mot) : « Née en 1990 », « Born in 1985 », « (FR, 1983) », « (1935–1995) »."""
    head = text[:600]
    m = BORN_FR.search(head)
    if m:
        return int(m.group(2)), m.group(1)
    m = BORN_EN.search(head)
    if m:
        return int(m.group(1)), None
    m = BORN_PAREN.search(head)
    if m:
        return int(m.group(1)), None
    m = LIFE.search(head[:300])
    if m:
        return int(m.group(1)), None
    return None, None


def images_of(scope, base, og=None, limit=5):
    out = []

    def add(u, cap=""):
        if not u:
            return
        u = urljoin(base, u.strip())
        low = u.lower()
        if not (IMG_EXT.search(low) or "artlogic" in low or "cloudinary" in low or "imgix" in low):
            return
        if BAD_IMG.search(low.rsplit("/", 1)[-1]) or low.endswith(".svg"):
            return
        key = os.path.basename(urlparse(u).path).lower()
        if any(k == key for k, _, _ in out):
            return
        out.append((key, u, clean(cap)[:200]))

    for img in scope.find_all("img"):
        try:
            w = int(str(img.get("width") or "0").rstrip("px") or 0)
        except ValueError:
            w = 0
        if 0 < w < 150:
            continue
        src = (img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
               or best_srcset(img.get("data-srcset") or img.get("srcset")) or img.get("src"))
        fig = img.find_parent("figure")
        cap = clean(fig.find("figcaption").get_text(" ")) if fig and fig.find("figcaption") else ""
        add(src, cap or img.get("alt") or img.get("title") or "")
        if len(out) >= limit:
            break
    if og and len(out) < 2:
        add(og)
    return [{"img": u, "cap": c} for _, u, c in out[:limit]]


def parse_page(url, name_hint=None, extra_images_url=None):
    soup, final = soup_of(url)
    if not soup:
        return None
    name = name_hint or name_from(soup)
    og_img = meta(soup, "og:image")
    og_desc = meta(soup, "og:description") or meta(soup, "description")
    for t in soup.find_all(["script", "style", "noscript", "nav", "header", "footer", "form", "iframe", "svg"]):
        t.decompose()
    scope = soup.find("main") or soup.find("article") or soup.body or soup
    paras, total = [], 0
    for p in scope.find_all("p"):
        t = clean(p.get_text(" "))
        if len(t) < 60 or BOILER.search(t) or t in paras:
            continue
        em = p.find(["em", "i"])
        if em and clean(em.get_text(" ")) == t and paras:   # traduction en italique après le texte
            continue
        paras.append(t)
        total += len(t)
        if len(paras) >= 6 or total > 1400:
            break
    bio = "\n".join(paras)
    if len(bio) < 80 and og_desc:
        bio = og_desc
    works = images_of(scope, final, og_img)
    if extra_images_url and len(works) < 4:
        s2, f2 = soup_of(extra_images_url)
        if s2:
            have = {os.path.basename(urlparse(w["img"]).path).lower() for w in works}
            more = [w for w in images_of(s2.find("main") or s2.body or s2, f2, None, limit=6)
                    if os.path.basename(urlparse(w["img"]).path).lower() not in have]
            works = (works + more)[:5]
    text_for_dates = clean(soup.get_text(" "))[:2000]
    born, word = born_of(bio + " " + text_for_dates)
    return {"name": name, "bio": bio[:1500], "works": works, "born": born, "bornWord": word, "url": final}


# ------------------------------------------------------------------ Beaux-Arts de Paris : L'Atlas
KEEP_GALLERY = re.compile(r"prix|lauréat|laureat|bourse", re.I)
TILE_HEADINGS = ["h2", "h3", "h4", "h5", "h6"]


def atlas_items(src):
    atlas, final = soup_of(src["index"])
    if not atlas:
        return []
    galleries = []
    for a in atlas.find_all("a", href=True):
        img = a.find("img")
        if not img or "/sites/default/files/" not in (img.get("src") or ""):
            continue
        u = urljoin(final, a["href"].strip())
        if same_site(u, final) and "/Oeuvre/" not in u and u not in galleries:
            galleries.append(u)
    items = []
    for g in galleries:
        gs, gfinal = soup_of(g)
        if not gs:
            continue
        gtitle = meta(gs, "og:title") or (gs.title.get_text().split("|")[0] if gs.title else "")
        gtitle = clean(gtitle)
        if not KEEP_GALLERY.search(gtitle):
            continue
        ym = re.search(r"(20\d\d)", gtitle)
        found = {}
        for a in gs.select('a[href*="/Oeuvre/"]'):
            href = urljoin(gfinal, a["href"].strip())
            t = found.setdefault(href, {"url": href, "name": "", "prize": "", "cap": "", "img": None})
            img = a.find("img")
            if img and not t["img"] and IMG_EXT.search(img.get("src") or ""):
                t["img"] = urljoin(gfinal, img["src"].strip())
            node, block = a, None
            for _ in range(6):
                parent = node.parent
                if parent is None:
                    break
                hrefs = {urljoin(gfinal, x["href"].strip()) for x in parent.select('a[href*="/Oeuvre/"]')}
                if len(hrefs) > 1:
                    break
                node = parent
                if block is None and node.find(TILE_HEADINGS):
                    block = node
            if block is not None and not t["name"]:
                heads = [clean(h.get_text()) for h in block.find_all(TILE_HEADINGS) if clean(h.get_text())]
                if heads:
                    t["name"] = heads[0]
                    t["prize"] = heads[1] if len(heads) > 1 else ""
                txt = clean(block.get_text(" "))
                for h in heads:
                    txt = txt.replace(h, " ")
                t["cap"] = clean(txt.replace("Loading...", ""))[:220]
        for t in found.values():
            t["note"] = ", ".join(x for x in (t["prize"], gtitle) if x)
            t["year"] = int(ym.group(1)) if ym else None
            items.append(t)
    return items


# ------------------------------------------------------------------ galeries du CPGA
SOCIAL = re.compile(r"facebook|instagram|linkedin|twitter|x\.com|youtube|vimeo|tiktok|pinterest|cdn-cgi|"
                    r"google\.|wa\.me|artsy\.net|comitedesgaleriesdart", re.I)
NAV_ARTISTS = re.compile(r"^\s*(?:nos |les )?(?:artist(?:e)?s|artistes? représentés|represented artists)\s*$", re.I)
NOT_ARTIST_SEG = re.compile(r"^(page|category|categorie|tag|feed|wp-json|search|recherche|a-z|filter|filtre)$", re.I)


def cpga_galleries(src):
    todo, visited, gal = [src["index"]], set(), {}
    while todo and len(visited) < 20:
        u = todo.pop(0)
        if u in visited:
            continue
        visited.add(u)
        sp, final = soup_of(u)
        if not sp:
            continue
        for l, text in links(sp, final):
            if not same_site(l, final):
                continue
            path = urlparse(l).path
            m = re.match(r"^/(?:fr/|en/)?galeries/([^/]+)/?$", path)
            if m and m.group(1) != "page" and not urlparse(l).query:
                gal.setdefault(m.group(1), l)
            if re.search(r"/galeries/page/\d+/?$", path) and l not in visited:
                todo.append(l)
    return gal


def gallery_info(url):
    sp, final = soup_of(url)
    if not sp:
        return None
    name = clean(sp.find("h1").get_text(" ")) if sp.find("h1") else ""
    site = None
    for s in sp.find_all(string=re.compile(r"^\s*(Web|Site(?: web| internet)?)\s*:?\s*$", re.I)):
        a = s.find_next("a", href=True)
        if a and a["href"].startswith("http") and not SOCIAL.search(a["href"]):
            site = a["href"]
            break
    if not site:
        for l, _ in links(sp, final):
            if l.startswith("http") and not same_site(l, final) and not SOCIAL.search(l):
                site = l
                break
    return {"name": name, "site": site}


def artist_links_from_index(index_url, artlogic=False):
    sp, final = soup_of(index_url)
    if not sp:
        return []
    root = root_of(final)
    out = []
    if artlogic:
        for l, _ in links(sp, final):
            m = re.match(r"^((?:/[a-z]{2})?/artists/\d+-[^/]+)/?", urlparse(l).path)
            if m and same_site(l, final):
                out.append(root + m.group(1) + "/overview/")
        return dedupe(out)
    ipath = urlparse(final).path.rstrip("/")
    if not ipath:
        return []
    prefixes = [ipath + "/"]
    last = ipath.rsplit("/", 1)[-1]
    if last.endswith("s") and len(last) > 3:
        prefixes.append(ipath[: -1] + "/")   # /artistes -> /artiste/
    for l, _ in links(sp, final):
        if not same_site(l, final) or urlparse(l).query:
            continue
        path = urlparse(l).path
        for pre in prefixes:
            if path.startswith(pre) and path.rstrip("/") != ipath:
                seg = path[len(pre):].strip("/").split("/")[0]
                if seg and not NOT_ARTIST_SEG.match(seg) and not seg.isdigit():
                    out.append(root + pre + seg + "/")
                break
    return dedupe(out)


def find_artist_index(site):
    sp, final = soup_of(site)
    if not sp:
        return None
    if "artlogic" in meta(sp, "generator").lower():
        return {"index": root_of(final) + "/artists/", "artlogic": True}
    for l, text in links(sp, final):
        if same_site(l, final) and NAV_ARTISTS.match(text or ""):
            return {"index": l, "artlogic": False}
    for p in ("/artistes/", "/artists/", "/fr/artistes/", "/en/artists/", "/fr/artists/"):
        cand = root_of(final) + p
        if len(artist_links_from_index(cand)) >= 3:
            return {"index": cand, "artlogic": False}
    return {"index": None, "artlogic": False}


# ------------------------------------------------------------------ état et fichiers
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)


def stale(ts, days):
    return not ts or NOW - ts > days * 86400


def existing_records():
    recs = {}
    if os.path.isdir(ADIR):
        for fn in os.listdir(ADIR):
            if fn.endswith(".json"):
                r = load_json(os.path.join(ADIR, fn), None)
                if r and r.get("id"):
                    recs[r["id"]] = r
    return recs


# ------------------------------------------------------------------ programme principal
def main():
    stats.clear()
    state = load_json(STATE, {})
    state.setdefault("sources", {})
    state.setdefault("galleries", {})
    state.setdefault("pages", {})
    recs = existing_records()
    names = {norm(r["name"]) for r in recs.values()}
    queues = {}   # source -> liste de tâches (url, infos)

    # 1) sources listées
    for src in SOURCES:
        if out_of_time():
            break
        key = src["key"]
        st = state["sources"].setdefault(key, {})
        if src["type"] == "atlas":
            if stale(st.get("t"), INDEX_REFRESH_DAYS) or not st.get("items"):
                items = atlas_items(src)
                if items:
                    st.update(t=NOW, items=items)
            queues[key] = [{"url": it["url"], "src": key, "fam": src["family"], "srcName": src["name"],
                            "name": it.get("name"), "note": it.get("note"), "year": it.get("year"),
                            "tile": {"img": it.get("img"), "cap": it.get("cap")}}
                           for it in st.get("items", [])]
        elif src["type"] == "list":
            if stale(st.get("t"), INDEX_REFRESH_DAYS) or not st.get("urls"):
                urls = []
                sp, final = soup_of(src["index"])
                pat = re.compile(src["pattern"])
                if sp:
                    urls += [l for l, _ in links(sp, final) if same_site(l, src["index"]) and pat.search(urlparse(l).path)]
                urls += sitemap_urls(src["index"], src["pattern"])
                urls = dedupe(urls)
                if urls:
                    st.update(t=NOW, urls=urls)
            queues[key] = [{"url": u, "src": key, "fam": src["family"], "srcName": src["name"]} for u in st.get("urls", [])]
        elif src["type"] == "cpga":
            if stale(st.get("t"), GALLERY_REFRESH_DAYS) or not st.get("galleries"):
                gal = cpga_galleries(src)
                if gal:
                    st.update(t=NOW, galleries=gal)
            # galeries à (re)découvrir cette nuit
            todo = sorted(st.get("galleries", {}).items(),
                          key=lambda kv: state["galleries"].get(kv[0], {}).get("t", 0))
            done = 0
            for slug, gurl in todo:
                if out_of_time() or done >= GALLERIES_PER_NIGHT:
                    break
                g = state["galleries"].setdefault(slug, {})
                if not stale(g.get("t"), INDEX_REFRESH_DAYS):
                    continue
                done += 1
                if stale(g.get("info_t"), GALLERY_REFRESH_DAYS) or "site" not in g:
                    info = gallery_info(gurl)
                    if info:
                        g.update(name=info["name"] or slug, site=info["site"], info_t=NOW)
                        g.pop("index", None)
                if g.get("site") and not g.get("index"):
                    idx = find_artist_index(g["site"])
                    if idx:
                        g.update(index=idx["index"], artlogic=idx["artlogic"])
                if g.get("index"):
                    urls = artist_links_from_index(g["index"], g.get("artlogic"))
                    if urls:
                        g["urls"] = urls
                g["t"] = NOW
            for slug, g in state["galleries"].items():
                if g.get("urls"):
                    queues["g-" + slug] = [{"url": u, "src": "g-" + slug, "fam": "galeries",
                                            "srcName": g.get("name") or slug, "artlogic": g.get("artlogic")}
                                           for u in g["urls"]]

    # 2) nouvelles fiches, à tour de rôle entre les sources
    for q in queues.values():
        q[:] = [t for t in q if t["url"] not in state["pages"]
                or (state["pages"][t["url"]].get("x") and stale(state["pages"][t["url"]].get("t"), RETRY_REJECTED_DAYS))]
    img_counts = {}
    for r in recs.values():
        c = img_counts.setdefault(r["src"], {})
        for w in r.get("works", []):
            c[img_key(w["img"])] = c.get(img_key(w["img"]), 0) + 1
    per_source = {k: 0 for k in queues}
    new_pages = 0
    active = [k for k in queues if queues[k]]
    while active and new_pages < MAX_NEW_PAGES and not out_of_time():
        for k in list(active):
            if not queues[k] or per_source[k] >= MAX_NEW_PER_SOURCE or new_pages >= MAX_NEW_PAGES:
                active.remove(k)
                continue
            task = queues[k].pop(0)
            per_source[k] += 1
            new_pages += 1
            rec = build_record(task, img_counts)
            ok = rec is not None and norm(rec["name"]) not in names
            if ok:
                names.add(norm(rec["name"]))
                recs[rec["id"]] = rec
                save_json(os.path.join(ADIR, rec["id"] + ".json"), rec)
                stats[k] = stats.get(k, 0) + 1
            state["pages"][task["url"]] = {"t": NOW, "id": rec["id"]} if ok else {"t": NOW, "x": 1}

    # 3) index léger pour l'app
    fam_of = {s["key"]: s["family"] for s in SOURCES}
    index = {
        "checked": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "artists": [{"id": r["id"], "s": r["src"], "f": r.get("fam") or fam_of.get(r["src"], "galeries"),
                     "b": r.get("born"), "ba": 1 if r.get("ba") else 0, "n": r["name"]}
                    for r in sorted(recs.values(), key=lambda r: r["id"])],
    }
    save_json(INDEX, index, compact=True)
    save_json(STATE, state)

    fams = {}
    for a in index["artists"]:
        fams[a["f"]] = fams.get(a["f"], 0) + 1
    log(f"Cette nuit : {new_pages} fiches lues, {sum(stats.values())} artistes ajoutés.")
    for k, n in sorted(stats.items()):
        log(f"  + {n:3d}  {k}")
    log(f"Total : {len(index['artists'])} artistes " + ", ".join(f"{f} {n}" for f, n in sorted(fams.items())))
    gal_found = sum(1 for g in state["galleries"].values() if g.get("urls"))
    log(f"Galeries avec une liste d'artistes trouvée : {gal_found} / {len(state['galleries'])}")
    return 0


def img_key(u):
    return os.path.basename(urlparse(u).path).lower()


def build_record(task, img_counts):
    url = task["url"]
    if task["src"] == "atlas":
        info = parse_page(url, name_hint=task.get("name"))
    else:
        works_url = url.replace("/overview/", "/works/") if task.get("artlogic") else None
        info = parse_page(url, extra_images_url=works_url)
    if not info or not info.get("name"):
        return None
    works = info["works"]
    tile = task.get("tile") or {}
    if tile.get("img"):
        key = os.path.basename(urlparse(tile["img"]).path).lower()
        works = [{"img": tile["img"], "cap": tile.get("cap") or ""}] + \
                [w for w in works if os.path.basename(urlparse(w["img"]).path).lower() != key]
    counts = img_counts.setdefault(task["src"], {})
    works = [w for w in works if counts.get(img_key(w["img"]), 0) < 2][:5]
    if not works:
        return None
    for w in works:
        counts[img_key(w["img"])] = counts.get(img_key(w["img"]), 0) + 1
    born = info["born"]
    if born and born < MIN_BORN:
        return None
    rid = slugify(task["src"], 40) + "--" + slugify(info["name"], 80)
    return {
        "id": rid,
        "src": task["src"],
        "fam": task["fam"],
        "srcName": task["srcName"],
        "name": info["name"],
        "born": born,
        "bornWord": info["bornWord"],
        "ba": bool(BA_RE.search(info["bio"])) or task["src"] == "atlas",
        "note": task.get("note") or task["srcName"],
        "year": task.get("year"),
        "bio": info["bio"],
        "works": works,
        "url": info["url"],
    }


if __name__ == "__main__":
    sys.exit(main())
