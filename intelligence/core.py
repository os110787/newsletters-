"""Dependencies-light ingestion, deduplication, scoring, persistent storage and draft curation."""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

UTC = timezone.utc
USER_AGENT = "VISIWING-Intelligence/0.1 (+https://visiwing.com; feed research)"
MAX_FEED_BYTES = 2_000_000
TOPIC_TERMS = {
    "AI Visibility & GEO": ["geo", "generative engine", "ai visibility", "ai search", "chatgpt", "perplexity", "llm", "ai overview", "ai mode", "citation", "answer engine", "gemini"],
    "SEO & Google": ["google", "ranking", "search console", "indexing", "search central", "core update", "seo", "serp", "crawler", "crawl"],
    "Tools": ["tool", "release", "github", "api", "platform", "plugin", "software", "audit", "dashboard"],
    "Technical SEO": ["schema", "structured data", "robots.txt", "sitemap", "javascript", "rendering", "crawl budget", "web vitals"],
    "Content & Authority": ["content", "authority", "backlink", "quality", "e-e-a-t", "guideline", "editorial", "brand"],
    "Local & E-Commerce": ["local", "shopping", "product", "merchant", "business profile", "ecommerce"],
    "Multimodal & Social": ["lens", "multimodal", "image search", "video", "youtube", "reddit", "tiktok"],
    "Research": ["study", "experiment", "benchmark", "paper", "research", "measurement"],
}
WEIGHTS = {"AI Visibility & GEO": 1.25, "SEO & Google": 1.2, "Tools": 1.0, "Technical SEO": 1.1,
           "Content & Authority": 1.05, "Local & E-Commerce": 1.0, "Multimodal & Social": 1.0, "Research": 1.15}
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "source"}


def now_utc() -> datetime:
    return datetime.now(UTC)


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("External article URL must be HTTP(S) with a hostname")
    netloc = p.hostname.lower() + (f":{p.port}" if p.port else "")
    args = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
    return urlunsplit((p.scheme.lower(), netloc, p.path.rstrip("/") or "/", urlencode(sorted(args)), ""))


def parse_date(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(s)
        except (ValueError, TypeError, IndexError):
            return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def strip_markup(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s or ""))).strip()[:2500]


def x_text(node: ET.Element, *paths: str) -> str:
    for path in paths:
        child = node.find(path)
        if child is not None:
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def parse_feed(data: bytes, source: dict) -> list[dict]:
    """Parse RSS2, RDF RSS and Atom. Never execute feed markup."""
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("Feed too large")
    root = ET.fromstring(data)
    atom = "{http://www.w3.org/2005/Atom}"
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"
    items = root.findall(".//item") or root.findall(".//{http://purl.org/rss/1.0/}item") or root.findall(f".//{atom}entry")
    results = []
    for node in items:
        title = strip_markup(x_text(node, "title", f"{atom}title"))
        link = x_text(node, "link")
        if not link:
            for a in node.findall(f"{atom}link"):
                if a.attrib.get("rel", "alternate") == "alternate":
                    link = a.attrib.get("href", "")
                    break
        description = strip_markup(x_text(node, "description", "summary", f"{atom}summary", f"{atom}content", f"{content_ns}encoded"))
        date = parse_date(x_text(node, "pubDate", "{http://purl.org/dc/elements/1.1/}date", f"{atom}published", f"{atom}updated"))
        if not title or not link:
            continue
        try:
            link = canonical_url(link)
        except ValueError:
            continue
        results.append({"title": title[:320], "url": link, "description": description,
                        "published_at": date, "source_id": source["id"], "source": source["name"],
                        "source_trust": int(source.get("trust", 60)), "source_category": source.get("category", "Other")})
    return results


def categorize(title: str, description: str, fallback: str) -> tuple[str, list[str]]:
    haystack = f"{title} {description}".casefold()
    matched = {}
    for topic, words in TOPIC_TERMS.items():
        hits = [w for w in words if w in haystack]
        if hits:
            matched[topic] = (sum(2 if w in title.casefold() else 1 for w in hits) * WEIGHTS[topic], hits)
    if not matched:
        return fallback, []
    topic = max(matched, key=lambda k: matched[k][0])
    return topic, matched[topic][1]


def score_item(item: dict, as_of: datetime | None = None) -> dict:
    """Transparent heuristic triage score, NOT independently verified impact."""
    as_of = as_of or now_utc()
    topic, hits = categorize(item["title"], item.get("description", ""), item.get("source_category", "Other"))
    trust = max(0, min(100, int(item.get("source_trust", 50))))
    if item.get("published_at"):
        age = max(0, (as_of - datetime.fromisoformat(item["published_at"])).days)
        recency = max(0, 100 - 7 * age)
    else:
        recency = 0  # unknown publication date must not be misrepresented as new
    relevance = min(100, 18 * len(hits) + (25 if topic in ("AI Visibility & GEO", "SEO & Google") else 10))
    practical = 70 if any(w in f"{item['title']} {item.get('description','')}".lower() for w in
                          ["tool", "guide", "update", "how to", "report", "feature", "launch", "release"]) else 40
    # weights sum to 100. Evidence rating reflects source quality, not claim-level verification.
    score = round(.25 * relevance + .25 * practical + .20 * relevance + .15 * trust + .10 * recency + .05 * 80)
    item = dict(item)
    item.update({"topic": topic, "matched_terms": hits, "score": min(100, score),
                 "evidence_status": "source_only", "review_status": "pending",
                 "summary": "", "action": "", "editorial_note": "Quelle und Auswirkungen vor Veröffentlichung prüfen."})
    return item


def make_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:24]


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS articles(
          id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL, source TEXT NOT NULL,
          source_id TEXT NOT NULL, source_trust INTEGER NOT NULL, source_category TEXT NOT NULL,
          description TEXT NOT NULL, published_at TEXT, discovered_at TEXT NOT NULL,
          topic TEXT NOT NULL, score INTEGER NOT NULL, evidence_status TEXT NOT NULL,
          review_status TEXT NOT NULL, summary TEXT NOT NULL, action TEXT NOT NULL,
          editorial_note TEXT NOT NULL, matched_terms TEXT NOT NULL)""")
        self.conn.commit()

    def upsert(self, item: dict) -> bool:
        url = canonical_url(item["url"])
        article_id = make_id(url)
        if self.conn.execute("SELECT 1 FROM articles WHERE id=?", (article_id,)).fetchone():
            return False
        scored = score_item(item)
        with self.conn:
            self.conn.execute("""INSERT INTO articles(id,url,title,source,source_id,source_trust,source_category,
              description,published_at,discovered_at,topic,score,evidence_status,review_status,summary,action,
              editorial_note,matched_terms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (article_id, url, scored["title"], scored["source"], scored["source_id"], scored["source_trust"],
               scored["source_category"], scored.get("description", ""), scored.get("published_at"),
               now_utc().isoformat(timespec="seconds"), scored["topic"], scored["score"],
               scored.get("evidence_status", "source_only"), scored.get("review_status", "pending"),
               scored.get("summary", ""), scored.get("action", ""), scored.get("editorial_note", ""),
               json.dumps(scored.get("matched_terms", []), ensure_ascii=False)))
        return True

    def list(self, days: int | None = 14, limit: int = 250, reviewed_only: bool = False) -> list[dict]:
        conditions = []
        args = []
        if days is not None:
            cutoff = (now_utc() - timedelta(days=days)).isoformat()
            conditions.append("published_at IS NOT NULL AND published_at>=?")
            args.append(cutoff)
        if reviewed_only:
            conditions.append("review_status='approved'")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        args.append(limit)
        rows = self.conn.execute(f"SELECT * FROM articles {where} ORDER BY score DESC,published_at DESC LIMIT ?", args).fetchall()
        return [dict(row) | {"matched_terms": json.loads(row["matched_terms"])} for row in rows]

    def change_review(self, article_id: str, status: str) -> bool:
        if status not in ("approved", "rejected", "pending"):
            raise ValueError("Invalid review status")
        with self.conn:
            result = self.conn.execute("UPDATE articles SET review_status=? WHERE id=?", (status, article_id))
        return result.rowcount > 0

    def export(self, path: str | Path) -> int:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        items = self.list(days=None, limit=10000)
        p.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        return len(items)

    def load(self, path: str | Path) -> int:
        p = Path(path)
        if not p.exists():
            return 0
        n = 0
        for item in json.loads(p.read_text(encoding="utf-8")):
            n += int(self.upsert(item))
        return n


def ingest(sources: list[dict], store: Store, *, timeout: float = 14.0) -> dict:
    result = {"sources_total": 0, "sources_ok": 0, "discovered": 0, "inserted": 0, "errors": []}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for source in sources:
            if not source.get("enabled", True):
                continue
            result["sources_total"] += 1
            try:
                response = client.get(source["url"])
                response.raise_for_status()
                if len(response.content) > MAX_FEED_BYTES:
                    raise ValueError("Feed exceeds size limit")
                entries = parse_feed(response.content, source)
                result["sources_ok"] += 1
                result["discovered"] += len(entries)
                for entry in entries:
                    result["inserted"] += int(store.upsert(entry))
            except (httpx.HTTPError, ET.ParseError, ValueError, sqlite3.Error) as exc:
                result["errors"].append({"source": source["id"], "error": str(exc)[:250]})
    return result


def load_sources(path: str | Path = "config/sources.json") -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Source catalog must be a JSON list")
    return data
