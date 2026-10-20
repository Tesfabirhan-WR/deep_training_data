#!/usr/bin/env python3
"""
Awate.com scraper -> JSON

Collects, for as many articles as possible:
    title, author, date of publication, column, comments, article text (+ url, id, categories)

How it works
  1. Tries the WordPress REST API (/wp-json/wp/v2/posts): fast, clean, ~30 requests
     for the whole archive. This is the preferred path.
  2. If the API is closed, falls back to crawling the listing pages (/page/N/) and
     parsing each article page (best effort: selectors are guesses, test with --limit 5).
  3. Comments are NOT in the page HTML: awate.com uses Disqus (forum "awate").
     They are only collected if you pass a Disqus public API key (--disqus-key).

Politeness: checks robots.txt, waits between requests, retries with back-off,
and saves progress regularly so you can stop and resume.

Install:  pip install requests beautifulsoup4
Test:     python awate_scraper.py --limit 5
Full run: python awate_scraper.py
With comments: python awate_scraper.py --disqus-key YOUR_PUBLIC_KEY
Authors only:  python awate_scraper.py --fill-authors   (fills authors in an existing JSON)
Text only:     python awate_scraper.py --fill-content   (adds the article text to an existing JSON)
"""
import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib import robotparser
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://awate.com"
API = BASE + "/wp-json/wp/v2"
DISQUS_FORUM = "awate"
UA = "awate-scraper/1.0 (personal study project; contact: greetaiates@gmail.com)"

# "Column" = one of the site's named columns (the "Columns" menu).
# Slugs seen on the site; edit this list after checking the Columns menu.
COLUMN_SLUGS = {"negarit", "perspective", "pencil", "pointblank", "hidden-promise",
                "alnahda", "u-turn", "tebeges"}

# Pages that look like articles in listings but are not (HTML fallback only).
NOT_ARTICLE = {"category", "author", "tag", "page", "wp", "links-3", "about-us",
               "posting-guidelines", "wp-json", "feed"}

MONTHS = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December")
DATE_RE = re.compile(rf"({MONTHS}) (\d{{1,2}}), (\d{{4}})")


# --------------------------------------------------------------------------- HTTP
class Client:
    """requests.Session with robots.txt check, delay between calls and retries."""

    def __init__(self, delay):
        self.delay = delay
        self.last = 0.0
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
        self.rp = robotparser.RobotFileParser()
        self.robots_ok = False
        try:
            r = self.s.get(BASE + "/robots.txt", timeout=20)
            if r.status_code == 200:
                self.rp.parse(r.text.splitlines())
                self.robots_ok = True
        except requests.RequestException:
            pass

    def allowed(self, url):
        return self.rp.can_fetch(UA, url) if self.robots_ok else True

    def get(self, url, params=None, check_robots=True, retries=4):
        if check_robots and not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        for attempt in range(retries):
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                print(f"  network error ({e}); retry {attempt + 1}/{retries}")
                time.sleep(2 ** attempt * 3)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                pause = int(r.headers.get("Retry-After", 0) or 0) or 2 ** attempt * 5
                print(f"  HTTP {r.status_code}; waiting {pause}s")
                time.sleep(pause)
                continue
            return r
        raise RuntimeError(f"giving up on {url}")


# ------------------------------------------------------------------------ helpers
def clean(text):
    """HTML entities and tags -> plain text."""
    return BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ", strip=True)


def month_date_to_iso(text):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    return datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat()


def pick_column(categories, columns_parent_id=None):
    for c in categories:
        if c["slug"] in COLUMN_SLUGS or (columns_parent_id and c.get("parent") == columns_parent_id):
            return c["name"]
    return None


def disqus_url(url):
    return f"https://{DISQUS_FORUM}.disqus.com/?url={quote(url, safe='')}"


def html_to_text(fragment):
    """Article HTML -> plain text, one block (paragraph, heading, list item) per paragraph."""
    soup = BeautifulSoup(fragment or "", "html.parser")
    for t in soup(["script", "style", "noscript", "iframe", "form", "figure"]):
        t.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    blocks = []
    for t in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre"]):
        if t.name == "li" and t.find("p"):
            continue                              # its <p> children are collected anyway
        text = html.unescape(t.get_text())
        lines = [re.sub(r"[ \t\r\f\v\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
        text = "\n".join(ln for ln in lines if ln)
        if text and text != "Advertisement":
            blocks.append(text)
    if not blocks:                                # plain text without block tags
        blocks = [re.sub(r"\s+", " ", soup.get_text(" ", strip=True))]
    return "\n\n".join(b for b in blocks if b)


# ----------------------------------------------------------------- Disqus comments
def disqus_comments(c, url, key):
    """All comments of one article via the Disqus API. [] if the thread has none."""
    out, cursor = [], None
    while True:
        params = {"api_key": key, "forum": DISQUS_FORUM, "thread": f"link:{url}",
                  "limit": 100, "order": "asc"}
        if cursor:
            params["cursor"] = cursor
        r = c.get("https://disqus.com/api/3.0/threads/listPosts.json", params=params,
                  check_robots=False)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Disqus returned non-JSON (HTTP {r.status_code})")
        if data.get("code") == 2:          # thread not found = no comments yet
            return out
        if data.get("code") != 0:
            raise RuntimeError(f"Disqus error: {data.get('response')}")
        for p in data["response"]:
            out.append({
                "id": p.get("id"),
                "parent": p.get("parent"),
                "author": (p.get("author") or {}).get("name"),
                "date": p.get("createdAt"),
                "text": p.get("raw_message"),
                "likes": p.get("likes"),
            })
        cur = data.get("cursor") or {}
        if not cur.get("hasNext"):
            return out
        cursor = cur.get("next")


# ------------------------------------------------------------------ REST API path
def rest_available(c):
    try:
        r = c.get(API + "/posts", params={"per_page": 1})
        return r.status_code == 200 and isinstance(r.json(), list)
    except Exception:
        return False


def fetch_categories(c):
    cats, page = {}, 1
    while True:
        r = c.get(API + "/categories", params={"per_page": 100, "page": page})
        if r.status_code != 200:
            break
        batch = r.json()
        for k in batch:
            cats[k["id"]] = {"name": clean(k["name"]), "slug": k["slug"], "parent": k["parent"]}
        if page >= int(r.headers.get("X-WP-TotalPages", 1)):
            break
        page += 1
    return cats


def iter_rest(c):
    cats = fetch_categories(c)
    columns_id = next((i for i, k in cats.items() if k["slug"] == "columns"), None)
    page, total = 1, 1
    while page <= total:
        r = c.get(API + "/posts", params={"per_page": 50, "page": page, "_embed": "author",
                                          "orderby": "date", "order": "desc"})
        if r.status_code != 200:
            print(f"REST page {page}: HTTP {r.status_code}, stopping")
            return
        total = int(r.headers.get("X-WP-TotalPages", 1))
        print(f"REST page {page}/{total}")
        for p in r.json():
            categories = [cats[i] for i in p.get("categories", []) if i in cats]
            author = None
            emb = p.get("_embedded", {}).get("author")
            if emb and isinstance(emb, list):
                author = emb[0].get("name")
            content = html_to_text((p.get("content") or {}).get("rendered", ""))
            yield {
                "id": p["id"],
                "url": p["link"],
                "title": clean(p["title"]["rendered"]),
                "author": author,
                "date": p["date"][:10],
                "column": pick_column(categories, columns_id),
                "categories": [k["name"] for k in categories],
                "content": content,
                "word_count": len(content.split()),
            }
        page += 1


# ------------------------------------------------------------------ HTML fallback
def iter_listing_urls(c):
    seen, page = set(), 1
    while True:
        url = BASE + "/" if page == 1 else f"{BASE}/page/{page}/"
        r = c.get(url)
        if r.status_code != 200:
            return
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for a in soup.select("h2 a[href], h3 a[href]"):
            href = a["href"].split("#")[0]
            parts = [x for x in urlparse(href).path.split("/") if x]
            if (urlparse(href).netloc.endswith("awate.com") and len(parts) == 1
                    and parts[0] not in NOT_ARTICLE and href not in seen):
                seen.add(href)
                new += 1
                yield href
        print(f"listing page {page}: {new} new links")
        if new == 0:
            return
        page += 1


def fill_content(c, articles, out):
    """Add the article body to records that have none, using the REST API (no per-article requests)."""
    missing = sum(1 for a in articles.values() if not a.get("content"))
    print(f"{missing} articles without content")
    if not rest_available(c):
        print("REST API unavailable: run a normal scrape with --no-rest --refresh instead")
        return
    filled = 0
    for rec in iter_rest(c):
        art = articles.get(rec["url"])
        if art is None:
            continue
        if not art.get("content") and rec["content"]:
            art["content"], art["word_count"] = rec["content"], rec["word_count"]
            filled += 1
        if not art.get("author") and rec.get("author"):
            art["author"] = rec["author"]
        if filled and filled % 200 == 0:
            save(out, articles)
    save(out, articles)
    left = sum(1 for a in articles.values() if not a.get("content"))
    print(f"Content done: {filled} filled, {left} still without content")


def norm(u):
    """Compare URLs without fragment or trailing slash."""
    return u.split("#")[0].rstrip("/")


def parse_listing_cards(soup):
    """{article url: author name} for every card on a listing page."""
    cards = {}
    for h in soup.select("h2 a[href], h3 a[href]"):
        href = h["href"].split("#")[0]
        parts = [x for x in urlparse(href).path.split("/") if x]
        if not (urlparse(href).netloc.endswith("awate.com") and len(parts) == 1
                and parts[0] not in NOT_ARTICLE):
            continue
        node = h
        for _ in range(6):                    # climb until the card container
            node = node.parent
            if node is None:
                break
            if len({x["href"] for x in node.select("h2 a[href], h3 a[href]")}) > 1:
                break                         # too far: we reached several cards
            authors = node.select('a[href*="/author/"]')
            if authors:
                cards[norm(href)] = authors[0].get_text(strip=True)
                break
    return cards


def fill_authors(c, articles, out):
    """Fill missing authors from the listing pages (about 140 requests in total)."""
    missing = {norm(u): u for u, a in articles.items() if not a.get("author")}
    print(f"{len(missing)} articles without author")
    page = 1
    while missing:
        url = BASE + "/" if page == 1 else f"{BASE}/page/{page}/"
        r = c.get(url)
        if r.status_code != 200:
            break
        cards = parse_listing_cards(BeautifulSoup(r.text, "html.parser"))
        if not cards:
            break
        hit = 0
        for key, author in cards.items():
            if key in missing:
                articles[missing.pop(key)]["author"] = author
                hit += 1
        print(f"listing page {page}: {hit} authors filled, {len(missing)} still missing")
        if page % 10 == 0:
            save(out, articles)
        page += 1
    save(out, articles)
    print(f"Authors done: {len(missing)} articles still without author")


def parse_article(c, url):
    r = c.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")

    og = soup.find("meta", property="og:title")
    title = clean(og["content"]) if og and og.get("content") else clean(soup.title.string)
    title = re.sub(r"\s*[–-]\s*Awate\.com$", "", title)

    # author: first /author/ link in the page body; date: the date written next to it
    a = soup.select_one('a[href*="/author/"]')
    author = a.get_text(strip=True) if a else None
    date = None
    if a and a.parent:
        date = month_date_to_iso(a.parent.get_text(" ", strip=True))
    if not date:
        t = soup.find("time")
        date = (t.get("datetime") or "")[:10] if t else None
    if not date:
        m = soup.find("meta", property="article:published_time")
        date = m["content"][:10] if m else None

    # category: the nearest /category/ link BEFORE the title that is not in the menu
    categories = []
    head = soup.find(lambda t: t.name in ("h1", "h2") and t.get_text(strip=True) == title)
    if head:
        for link in head.find_all_previous("a", href=re.compile(r"/category/")):
            if link.find_parent(["nav", "header"]):
                continue
            slug = [x for x in urlparse(link["href"]).path.split("/") if x][-1]
            categories.append({"name": link.get_text(strip=True), "slug": slug})
            break
    # body: paragraphs after the title, until the first block that is not article text
    stop_h2 = {"Latest", "Sections", "What people said", "More like this"}
    paras = []
    if head:
        for el in head.find_all_next(["p", "h2", "h3", "h4", "h5", "h6"]):
            txt = el.get_text(" ", strip=True)
            if el.name == "h2" and txt in stop_h2:
                break
            if txt.startswith("Tags:") or txt == "Donate":
                break
            if el.name == "p" and txt and txt != "Advertisement":
                paras.append(txt)
    content = "\n\n".join(paras)
    return {
        "id": None,
        "url": url,
        "title": title,
        "author": author,
        "date": date,
        "column": pick_column(categories),
        "categories": [k["name"] for k in categories],
        "content": content,
        "word_count": len(content.split()),
    }


# --------------------------------------------------------------------------- main
def load(path):
    if path.exists():
        try:
            return {a["url"]: a for a in json.loads(path.read_text(encoding="utf-8"))}
        except (ValueError, KeyError):
            print(f"Could not read {path}; starting fresh")
    return {}


def save(path, articles):
    rows = sorted(articles.values(), key=lambda a: a.get("date") or "", reverse=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description="Scrape awate.com articles to JSON")
    ap.add_argument("--out", default="awate_articles.json")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new articles (0 = all)")
    ap.add_argument("--disqus-key", default="", help="Disqus public API key, to collect comments")
    ap.add_argument("--no-rest", action="store_true", help="skip the REST API, use HTML only")
    ap.add_argument("--refresh", action="store_true", help="re-scrape articles already saved")
    ap.add_argument("--fill-authors", action="store_true",
                    help="only fill missing authors in the existing JSON file, then exit")
    ap.add_argument("--fill-content", action="store_true",
                    help="only add the article text to the existing JSON file, then exit")
    ap.add_argument("--no-fill", action="store_true",
                    help="do not fill missing authors at the end of a normal run")
    args = ap.parse_args()

    out = Path(args.out)
    articles = load(out)
    print(f"{len(articles)} articles already in {out}")
    c = Client(args.delay)
    if not c.robots_ok:
        print("Warning: could not read robots.txt")

    if args.fill_content:
        fill_content(c, articles, out)
        return
    if args.fill_authors:
        fill_authors(c, articles, out)
        return

    if not args.no_rest and rest_available(c):
        print("Using the WordPress REST API")
        source = iter_rest(c)
    else:
        print("REST API unavailable: crawling HTML pages (slower, best effort)")
        source = (("html", u) for u in iter_listing_urls(c))

    done = 0
    try:
        for item in source:
            if isinstance(item, tuple):                 # HTML path: item = ("html", url)
                url = item[1]
                if url in articles and not args.refresh:
                    continue
                try:
                    rec = parse_article(c, url)
                except Exception as e:
                    print(f"  skip {url}: {e}")
                    continue
            else:                                       # REST path
                rec = item
                if rec["url"] in articles and not args.refresh:
                    continue

            if args.disqus_key:
                try:
                    rec["comments"] = disqus_comments(c, rec["url"], args.disqus_key)
                except Exception as e:
                    print(f"  comments failed for {rec['url']}: {e}")
                    rec["comments"] = None
            else:
                rec["comments"] = None                  # not collected (see docstring)
            rec["comments_url"] = disqus_url(rec["url"])

            articles[rec["url"]] = rec
            done += 1
            print(f"[{len(articles)}] {rec['date']} | {rec['author']} | {rec['column']} | {rec['title'][:60]}")
            if done % 25 == 0:
                save(out, articles)
            if args.limit and done >= args.limit:
                break
    except KeyboardInterrupt:
        print("\nInterrupted: saving what we have")
    finally:
        save(out, articles)
    if not args.no_fill and any(not a.get("author") for a in articles.values()):
        fill_authors(c, articles, out)
    print(f"Done: {len(articles)} articles in {out}")


if __name__ == "__main__":
    sys.exit(main())
