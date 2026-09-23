"""
Robot de l'app Trois : chaque nuit, il va chercher sur le web les cartes du jour.

  - Contemporain : exploration en direct. Il tire une source au hasard (une galerie de l'annuaire
    du CPGA, L'Atlas des Beaux-Arts de Paris, le Réseau documents d'artistes, un prix), lit sa
    liste d'artistes et choisit un artiste jamais proposé, de préférence jeune.
  - Moderne et ancien : Wikidata et Wikipédia.
  - Histoires : Wikipédia (liste HISTOIRES de index.html, puis « Unusual articles »).

Pas de base de données : il écrit seulement data/jour/AAAA-MM-JJ.json (les 3 derniers jours)
et data/vus.json (ce qui a déjà été proposé, pour ne jamais se répéter).
Il respecte robots.txt et attend 1 seconde entre deux pages d'un même site.
"""
import json
import os
import random
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib import robotparser
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ RÉGLAGES
MIN_BORN = 1955              # contemporain : artistes nés avant cette année ignorés (quand l'année est connue)
BONUS_BEAUX_ARTS = 8         # en années : un diplômé des Beaux-Arts de Paris né en 1985 compte comme un né en 1993
FAMILLES = ["beauxarts", "galeries", "dda", "galeries", "prix"]   # rotation quotidienne (galeries 2 jours sur 5)
WIKIS_MIN, WIKIS_MAX = 2, 25 # moderne et ancien : notoriété (nombre de sites Wikimedia)
MODERNE_DES, MODERNE_AVANT = 1815, 1935
TIME_BUDGET_S = 12 * 60
KEEP_DAYS = 3

# Sources du contemporain. type "list" : page qui liste les artistes + motif des adresses de fiches.
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




# ------------------------------------------------------------------ API Wikimedia (pas de robots.txt : faites pour les programmes)
def api_json(url, params=None, timeout=30):
    if out_of_time():
        return None
    dom = urlparse(url).netloc
    wait = 1.0 - (time.time() - _last_hit.get(dom, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[dom] = time.time()
    try:
        r = session.get(url, params=params, timeout=timeout)
        if r.status_code != 200:
            log("  API", r.status_code, url[:80])
            return None
        return r.json()
    except Exception as e:
        log("  API erreur", type(e).__name__, url[:80])
        return None


def sparql(q, timeout=90):
    return api_json("https://query.wikidata.org/sparql", {"format": "json", "query": q}, timeout)


def wp(lang, **params):
    return api_json(f"https://{lang}.wikipedia.org/w/api.php",
                    {"format": "json", "formatversion": "2", **params})


def year_of(v):
    m = re.match(r"^(-?\d{1,4})-", v or "")
    return int(m.group(1)) if m else None


def clean_extract(s):
    s = re.sub(r"\(\s*[,;:]?\s*\)", "", s or "")
    s = re.sub(r"\[\d+\]", "", s)
    s = re.sub(r"[ \t\u00a0]{2,}", " ", s)
    s = re.sub(r" ([,.])", r"\1", s)
    return re.sub(r"\n{2,}", "\n", s).strip()


def wiki_page(lang, title, want_fr=False):
    prop = "extracts|pageprops|pageimages|info|description" + ("|langlinks" if want_fr else "")
    params = dict(action="query", redirects="1", titles=title, prop=prop, exintro="1", explaintext="1",
                  ppprop="wikibase_item|disambiguation", piprop="thumbnail", pithumbsize="900", inprop="url")
    if want_fr:
        params["lllang"] = "fr"
    d = wp(lang, **params)
    pages = (d or {}).get("query", {}).get("pages") or []
    if not pages:
        return None
    pg = pages[0]
    if pg.get("missing") or pg.get("invalid") or "disambiguation" in (pg.get("pageprops") or {}):
        return None
    ext = clean_extract(pg.get("extract"))
    if len(ext) < 40:
        return None
    return {
        "lang": lang, "title": pg["title"], "extract": ext, "desc": pg.get("description") or "",
        "qid": (pg.get("pageprops") or {}).get("wikibase_item"),
        "thumb": (pg.get("thumbnail") or {}).get("source"),
        "url": pg.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{quote(pg['title'].replace(' ', '_'))}",
        "fr": ((pg.get("langlinks") or [{}])[0]).get("title"),
    }


def commons(name, w):
    return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}?width={w}"


def wd_artist(qid):
    q = f"""SELECT ?b ?d ?frT ?enT ?artsy ?work ?workLabel ?img ?inc WHERE {{
  {{ wd:{qid} wdt:P569 ?b . }}
  UNION {{ wd:{qid} wdt:P570 ?d . }}
  UNION {{ ?fa schema:about wd:{qid} ; schema:isPartOf <https://fr.wikipedia.org/> ; schema:name ?frT . }}
  UNION {{ ?ea schema:about wd:{qid} ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?enT . }}
  UNION {{ wd:{qid} wdt:P2042 ?artsy . }}
  UNION {{ SELECT ?work ?img ?inc WHERE {{ ?work wdt:P170 wd:{qid} ; wdt:P18 ?img . OPTIONAL {{ ?work wdt:P571 ?inc . }} }} LIMIT 80 }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "fr,en". }}
}}"""
    d = sparql(q, 40)
    if not d:
        return None
    out = {"birth": None, "death": None, "fr": None, "en": None, "artsy": None, "works": []}
    seen = set()
    for r in d.get("results", {}).get("bindings", []):
        g = lambda k: r.get(k, {}).get("value")
        if g("b") and out["birth"] is None:
            out["birth"] = year_of(g("b"))
        if g("d") and out["death"] is None:
            out["death"] = year_of(g("d"))
        for k, dst in (("frT", "fr"), ("enT", "en"), ("artsy", "artsy")):
            if g(k) and not out[dst]:
                out[dst] = g(k)
        if g("work") and g("img") and g("work") not in seen and "Special:FilePath/" in g("img"):
            seen.add(g("work"))
            name = requests.utils.unquote(g("img").split("Special:FilePath/", 1)[1]).replace("_", " ")
            label = g("workLabel") or ""
            out["works"].append({"title": "" if re.match(r"^Q\d+$", label) else label, "year": year_of(g("inc")),
                                 "img": commons(name, 520), "full": commons(name, 1600),
                                 "page": "https://commons.wikimedia.org/wiki/File:" + quote(name.replace(" ", "_")),
                                 "file": name.lower()})
    return out


BAD_FILE = re.compile(r"logo|ic[oô]ne|icon|flag|drapeau|signature|blason|coat[ _]of[ _]arms|\bmap\b|carte|"
                      r"commons|wiki|symbol|portal|portail|question|stub|pictogram|nuvola|crystal_|edit-", re.I)


def article_images(lang, title):
    d = wp(lang, action="query", generator="images", titles=title, gimlimit="50", prop="imageinfo",
           iiprop="url|size|mime|extmetadata", iiurlwidth="520", iiextmetadatafilter="ObjectName",
           iiextmetadatalanguage=lang)
    out = []
    for p in (d or {}).get("query", {}).get("pages", []) or []:
        ii = (p.get("imageinfo") or [None])[0]
        if not ii or not re.match(r"^image/(jpeg|png|webp)$", ii.get("mime", "")) or ii.get("width", 0) < 300 \
                or ii.get("height", 0) < 200 or BAD_FILE.search(p["title"]):
            continue
        name = p["title"].split(":", 1)[-1]
        t = clean(BeautifulSoup((ii.get("extmetadata", {}).get("ObjectName", {}) or {}).get("value", "") or "",
                                "html.parser").get_text(" "))
        if not t or len(t) > 120:
            t = re.sub(r"\s*-\s*(WGA\d+|Google Art Project)", "", re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I)).strip()
        out.append({"title": t, "year": None, "img": ii.get("thumburl") or ii.get("url"),
                    "full": f"https://{lang}.wikipedia.org/wiki/Special:FilePath/{quote(name)}?width=1600",
                    "page": ii.get("descriptionurl"), "file": name.lower()})
    return out


def wd_pool(day_num):
    digit = day_num % 10
    q = f"""SELECT ?a (MIN(YEAR(?b)) AS ?y) WHERE {{
  {{ ?a wdt:P245 [] . }} UNION {{ ?a wdt:P2042 [] . }}
  FILTER(STRENDS(STR(?a), "{digit}"))
  ?a wikibase:sitelinks ?sl .
  FILTER(?sl >= {WIKIS_MIN} && ?sl <= {WIKIS_MAX})
  ?a wdt:P569 ?b .
  FILTER(isLiteral(?b))
  FILTER EXISTS {{ ?art schema:about ?a ; schema:isPartOf ?w . VALUES ?w {{ <https://fr.wikipedia.org/> <https://en.wikipedia.org/> }} }}
}}
GROUP BY ?a"""
    d = sparql(q, 120)
    eras = {"moderne": [], "ancien": []}
    for r in (d or {}).get("results", {}).get("bindings", []):
        qid = r.get("a", {}).get("value", "").rsplit("/", 1)[-1]
        try:
            y = int(r.get("y", {}).get("value"))
        except (TypeError, ValueError):
            continue
        if MODERNE_DES <= y < MODERNE_AVANT:
            eras["moderne"].append(qid)
        elif y < MODERNE_DES:
            eras["ancien"].append(qid)
    return eras


def pick_wikidata(era, pool, seen, rng):
    cands = [q for q in pool if q not in seen]
    rng.shuffle(cands)
    first = None
    for qid in cands[:8]:
        if out_of_time():
            break
        wd = wd_artist(qid)
        if not wd:
            continue
        page = (wd["fr"] and wiki_page("fr", wd["fr"])) or (wd["en"] and wiki_page("en", wd["en"]))
        if not page:
            continue
        works = sorted(wd["works"], key=lambda w: 0 if w["title"] else 1)
        rng.shuffle(works)
        works = sorted(works, key=lambda w: 0 if w["title"] else 1)[:4]
        if len(works) < 3:
            have = {w["file"] for w in works}
            works += [w for w in article_images(page["lang"], page["title"]) if w["file"] not in have]
            works = works[:4]
        has_works = bool(works)
        if not works and page["thumb"]:
            works = [{"title": "", "year": None, "img": page["thumb"], "full": page["thumb"], "page": page["url"]}]
        card = {"type": "artist", "era": era, "wd": qid, "id": page["lang"] + ":" + page["title"],
                "name": re.sub(r"\s*\([^)]*\)\s*$", "", page["title"]), "desc": page["desc"],
                "birth": wd["birth"], "death": wd["death"], "extract": page["extract"], "url": page["url"],
                "lang": page["lang"], "artsy": wd["artsy"], "works": works, "hasWorks": has_works,
                "thumb": works[0]["img"] if works else page["thumb"]}
        if has_works:
            return card
        first = first or card
    return first


# ------------------------------------------------------------------ contemporain : exploration en direct
def valid_card(info, src_name, note, url):
    if not info or not info.get("name") or not info.get("works"):
        return None
    if info["born"] and info["born"] < MIN_BORN:
        return None
    born = f"{info['bornWord'] or 'Né·e'} en {info['born']}" if info["born"] else ""
    return {"type": "artist", "era": "contemporain", "src": "c", "id": "c:" + url, "url": info["url"],
            "desc": ". ".join(x for x in (born, note) if x),
            "name": info["name"], "born": info["born"], "bornWord": info["bornWord"],
            "ba": bool(BA_RE.search(info["bio"] or "")), "srcName": src_name, "note": note,
            "extract": info["bio"], "lang": "fr",
            "works": [{"title": "", "cap": w["cap"], "year": None, "img": w["img"], "full": w["img"], "page": info["url"]}
                      for w in info["works"][:5]], "hasWorks": True, "thumb": info["works"][0]["img"]}


def drop_shared_images(cards):
    """Images présentes sur plusieurs fiches d'une même source (logo, image de partage) : retirées."""
    count = {}
    for c in cards:
        for w in c["works"]:
            k = os.path.basename(urlparse(w["img"]).path).lower()
            count[k] = count.get(k, 0) + 1
    out = []
    for c in cards:
        c["works"] = [w for w in c["works"] if count[os.path.basename(urlparse(w["img"]).path).lower()] < 2]
        if c["works"]:
            c["thumb"] = c["works"][0]["img"]
            out.append(c)
    return out


def try_urls(urls, seen, rng, src_name, note_of=lambda u: None, name_of=lambda u: None, extra=lambda u: None, want=3, max_tries=5):
    urls = [u for u in urls if "c:" + u not in seen]
    rng.shuffle(urls)
    cards = []
    for u in urls[:max_tries]:
        if out_of_time() or len(cards) >= want:
            break
        info = parse_page(u, name_hint=name_of(u), extra_images_url=extra(u))
        c = valid_card(info, src_name, note_of(u) or src_name, u)
        if c:
            cards.append(c)
    return drop_shared_images(cards) if len(cards) > 1 else cards


def family_candidates(fam, seen, rng):
    srcs = [s for s in SOURCES if s["family"] == fam]
    if fam == "beauxarts":
        for src in srcs:
            items = atlas_items(src)
            by_year = {}
            for it in items:
                if "c:" + it["url"] not in seen:
                    by_year.setdefault(it.get("year") or 0, []).append(it)
            for y in sorted(by_year, reverse=True):   # les promotions les plus récentes d'abord
                pool = by_year[y]
                meta_of = {it["url"]: it for it in pool}
                cards = try_urls([it["url"] for it in pool], seen, rng, src["name"],
                                 note_of=lambda u: meta_of[u].get("note"), name_of=lambda u: meta_of[u].get("name"))
                for c in cards:   # image et légende de la tuile de L'Atlas en premier
                    t = meta_of.get(c["id"][2:]) or {}
                    if t.get("img"):
                        k = os.path.basename(urlparse(t["img"]).path).lower()
                        rest = [w for w in c["works"] if os.path.basename(urlparse(w["img"]).path).lower() != k]
                        c["works"] = [{"title": "", "cap": t.get("cap") or "", "year": None, "img": t["img"],
                                       "full": t["img"], "page": c["url"]}] + rest[:4]
                        c["thumb"] = t["img"]
                    c["ba"] = True
                if cards:
                    return cards
        return []
    if fam == "galeries":
        src = srcs[0]
        gal = cpga_galleries(src)
        slugs = sorted(gal)
        rng.shuffle(slugs)
        for slug in slugs[:12]:
            if out_of_time():
                break
            info = gallery_info(gal[slug])
            if not info or not info.get("site"):
                continue
            idx = find_artist_index(info["site"])
            if not idx or not idx.get("index"):
                log("  pas de page Artistes trouvée :", info.get("name") or slug)
                continue
            urls = artist_links_from_index(idx["index"], idx.get("artlogic"))
            if len(urls) < 2:
                continue
            log(f"  galerie : {info.get('name') or slug} ({len(urls)} artistes)")
            works_of = (lambda u: u.replace("/overview/", "/works/")) if idx.get("artlogic") else (lambda u: None)
            cards = try_urls(urls, seen, rng, info.get("name") or slug, extra=works_of)
            if cards:
                return cards
        return []
    # dda, prix : liste ou plan du site
    rng.shuffle(srcs)
    for src in srcs:
        if out_of_time():
            break
        pat = re.compile(src["pattern"])
        urls = []
        sp, final = soup_of(src["index"])
        if sp:
            urls += [l for l, _ in links(sp, final) if same_site(l, src["index"]) and pat.search(urlparse(l).path)]
        if len(urls) < 10:
            urls += sitemap_urls(src["index"], src["pattern"])
        urls = dedupe(urls)
        log(f"  {src['name']} : {len(urls)} fiches")
        cards = try_urls(urls, seen, rng, src["name"])
        if cards:
            return cards
    return []


def pick_contemporary(day_num, seen, rng):
    for k in range(len(FAMILLES)):
        fam = FAMILLES[(day_num + k) % len(FAMILLES)]
        if out_of_time():
            break
        log(f"Contemporain : famille « {fam} »")
        cards = family_candidates(fam, seen, rng)
        if cards:   # le plus jeune (petit bonus Beaux-Arts de Paris)
            return max(cards, key=lambda c: (c["born"] or 1978) + (BONUS_BEAUX_ARTS if c["ba"] else 0))
    return None


# ------------------------------------------------------------------ histoires
def histoires_list():
    try:
        html = open("index.html", encoding="utf-8").read()
        m = re.search(r"const HISTOIRES = (\[.*?\]);", html, re.S)
        return json.loads(m.group(1)) if m else []
    except Exception as e:
        log("Liste HISTOIRES illisible :", e)
        return []


def unusual_titles():
    d = wp("en", action="query", list="allpages", apnamespace="4", apprefix="Unusual articles/", aplimit="40")
    titles = ["Wikipedia:Unusual articles"] + [p["title"] for p in (d or {}).get("query", {}).get("allpages", [])]
    out, cont = [], {}
    for _ in range(12):
        d = wp("en", action="query", prop="links", titles="|".join(titles[:50]), plnamespace="0", pllimit="max", **cont)
        if not d:
            break
        for pg in d.get("query", {}).get("pages", []):
            out += [l["title"] for l in pg.get("links", [])]
        if d.get("continue"):
            cont = d["continue"]
        else:
            break
    return [t for t in dict.fromkeys(out) if not t.startswith("List of")]


def load_story(entry):
    fr, _, en = entry.partition("|")
    fr = fr.strip() if fr.strip() not in ("", "-") else None
    en = en.strip() or None
    page = wiki_page("fr", fr) if fr else None
    if not page and en:
        pe = wiki_page("en", en, want_fr=True)
        page = (pe and pe.get("fr") and wiki_page("fr", pe["fr"])) or pe
    if not page:
        return None
    return {"type": "story", "id": page["lang"] + ":" + page["title"], "name": page["title"], "desc": page["desc"],
            "extract": page["extract"], "url": page["url"], "lang": page["lang"], "thumb": page["thumb"], "key": entry}


def pick_stories(seen, rng, n=3):
    own = histoires_list()
    rng_own = random.Random("trois-histoires")
    rng_own.shuffle(own)
    todo = [e for e in own if e not in seen]
    if len(todo) < n + 3:
        extra = ["-|" + t for t in unusual_titles()]
        known = {e.split("|")[-1] for e in own}
        extra = [e for e in extra if e not in seen and e.split("|")[-1] not in known]
        rng.shuffle(extra)
        todo += extra
    out = []
    for e in todo:
        if len(out) >= n or out_of_time():
            break
        s = load_story(e)
        if s and all(x["id"] != s["id"] for x in out):
            out.append(s)
    return out


# ------------------------------------------------------------------ programme principal
DAY_DIR = os.path.join("data", "jour")
SEEN = os.path.join("data", "vus.json")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def main():
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    key = today.isoformat()
    path = os.path.join(DAY_DIR, key + ".json")
    old = load_json(path, None)
    if old and len(old.get("artists", [])) == 3 and len(old.get("stories", [])) == 3 and not os.environ.get("FORCE"):
        log("Les cartes du", key, "sont déjà prêtes.")
        return 0
    seen = load_json(SEEN, {"c": [], "w": [], "s": []})
    day_num = today.toordinal()
    rng = random.Random("trois-" + key)

    log("== Contemporain")
    c = pick_contemporary(day_num, set(seen["c"]), rng)
    log("== Moderne et ancien (Wikidata)")
    pool = wd_pool(day_num)
    log(f"  réservoir : {len(pool['moderne'])} modernes, {len(pool['ancien'])} anciens")
    m = pick_wikidata("moderne", pool["moderne"], set(seen["w"]), rng)
    a = pick_wikidata("ancien", pool["ancien"], set(seen["w"]), rng)
    log("== Histoires")
    stories = pick_stories(set(seen["s"]), rng)

    artists = [x for x in (c, m, a) if x]
    day = {"date": key, "made": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="minutes"),
           "artists": artists if len(artists) == 3 else [], "stories": stories if len(stories) == 3 else []}
    os.makedirs(DAY_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(day, f, ensure_ascii=False, indent=1)
    if c:
        seen["c"].append(c["id"])
    seen["w"] += [x["wd"] for x in (m, a) if x]
    seen["s"] += [s["key"] for s in stories]
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)
    limit = (today - timedelta(days=KEEP_DAYS)).isoformat()
    for fn in os.listdir(DAY_DIR):
        if fn.endswith(".json") and fn[:-5] < limit:
            os.remove(os.path.join(DAY_DIR, fn))

    for x in artists:
        log(f"  {x['era']:12s} {x['name']}  ({x.get('srcName') or x.get('desc') or ''})")
    for s in stories:
        log(f"  histoire     {s['name']}")
    log(f"Terminé en {int(time.time() - START)} s.")
    return 0 if len(artists) == 3 and len(stories) == 3 else 1


if __name__ == "__main__":
    sys.exit(main())
