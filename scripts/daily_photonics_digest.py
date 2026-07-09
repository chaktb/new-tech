#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_photonics_digest.py
=========================
매일 아침 arXiv + 학술 RSS 에서 Si photonics / PIC / quantum computing 관련
새 논문·기사를 수집하고, 로컬 Ollama(oss-120b)로 한국어 요약을 붙여
new-tech 리포에 posts/YYYY-MM-DD.html 를 생성한 뒤 index.html 카드를 갱신하고
GitHub 에 push 한다. Cloudflare 가 push 를 감지해 자동 배포.

DGX Spark (zgx-1eba) 의 crontab 에서 매일 08:00 KST 실행.
"""

import os
import re
import sys
import json
import html
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import subprocess
from datetime import datetime, timezone, timedelta

# ============================================================
# CONFIG  ─  환경에 맞게 이 블록만 수정하면 됩니다
# ============================================================
REPO_DIR      = os.path.expanduser("~/new-tech")      # 로컬 클론 경로
GIT_BRANCH    = "main"
GIT_REMOTE    = "origin"

OLLAMA_URL    = "http://localhost:11434"              # 다른 머신이면 http://100.71.158.37:11434
OLLAMA_MODEL  = "gpt-oss:120b"                         # `ollama list` 로 정확한 태그 확인
OLLAMA_ENABLE = True                                   # False 면 요약 생략(제목+링크만)

MAX_ITEMS         = 8      # 하루 최대 게시 항목 수
ARXIV_MAX_PER_CAT = 15     # arXiv 카테고리별 검색 개수 (필터 전)
LOOKBACK_HOURS    = 30     # 최근 N시간 내 항목만 (매일 실행 기준 여유 6h)
KST               = timezone(timedelta(hours=9))

# arXiv 검색 카테고리 + 키워드
ARXIV_QUERIES = [
    "cat:physics.optics AND (silicon photonics OR photonic integrated)",
    "cat:quant-ph AND (photonic OR integrated OR quantum computing)",
    "cat:physics.app-ph AND (silicon photonics OR PIC)",
]

# 추가 학술/뉴스 RSS 피드
RSS_FEEDS = [
    "https://phys.org/rss-feed/physics-news/optics-photonics/",
    "https://phys.org/rss-feed/physics-news/quantum-physics/",
    "https://www.nature.com/nphoton.rss",   # Nature Photonics (current issue)
]

# 제목/초록 필터 키워드 (하나라도 포함되어야 채택)
KEYWORDS = re.compile(
    r"(silicon photonic|si photonic|photonic integrat|\bPIC\b|integrated photonic|"
    r"quantum comput|quantum photonic|waveguide|optical modulator|"
    r"single[- ]photon|photonic chip|LNOI|lithium niobate)",
    re.IGNORECASE,
)
# ============================================================


def log(msg):
    print(f"[{datetime.now(KST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- 수집: arXiv ----------
def fetch_arxiv(query, max_results):
    base = "http://export.arxiv.org/api/query"
    params = urllib.parse.urlencode({
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    })
    url = f"{base}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "new-tech-digest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()

    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    out = []
    for e in root.findall("a:entry", ns):
        title = " ".join(e.findtext("a:title", "", ns).split())
        summ = " ".join(e.findtext("a:summary", "", ns).split())
        link = e.findtext("a:id", "", ns)
        pub = e.findtext("a:published", "", ns)  # 2026-07-08T12:00:00Z
        try:
            dt = datetime.strptime(pub, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = datetime.now(timezone.utc)
        out.append({"title": title, "abstract": summ, "link": link,
                    "published": dt, "source": "arXiv"})
    return out


# ---------- 수집: RSS ----------
def fetch_rss(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "new-tech-digest/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        root = ET.fromstring(raw)
    except Exception as e:
        log(f"  RSS 실패 {url} -> {e}")
        return []

    out = []
    # dc namespace (Nature 등에서 사용)
    DC = "{http://purl.org/dc/elements/1.1/}date"
    src_name = _source_label(url)
    # RSS 2.0
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = re.sub("<[^>]+>", "", item.findtext("description") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        dc_raw = (item.findtext(DC) or "").strip()
        dt = _parse_rss_date(pub_raw, dc_raw)
        if title and link:
            out.append({"title": title, "abstract": desc[:600], "link": link,
                        "published": dt, "source": src_name})
    # Atom fallback
    if not out:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns):
            title = (e.findtext("a:title", "", ns) or "").strip()
            link_el = e.find("a:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            desc = re.sub("<[^>]+>", "", e.findtext("a:summary", "", ns) or "").strip()
            pub_raw = (e.findtext("a:updated", "", ns) or e.findtext("a:published", "", ns) or "").strip()
            dt = _parse_rss_date("", pub_raw)
            out.append({"title": title, "abstract": desc[:600], "link": link,
                        "published": dt, "source": src_name})
    return out


def _source_label(url):
    d = _domain(url)
    if "nature.com" in d:
        return "Nature Photonics"
    if "phys.org" in d:
        return "Phys.org"
    return d


def _parse_rss_date(pub_raw, dc_raw=""):
    # RFC 822 (pubDate)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(pub_raw, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    # ISO 8601 (dc:date / Atom updated), 예: 2026-07-03T00:00:00Z 또는 2026-07-03
    if dc_raw:
        s = dc_raw.replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                d = datetime.strptime(s, fmt)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return d.astimezone(timezone.utc)
            except ValueError:
                continue
    return datetime.now(timezone.utc)


def _domain(url):
    return urllib.parse.urlparse(url).netloc.replace("www.", "")


# ---------- 필터 ----------
def filter_items(items):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    seen_titles = set()
    kept = []
    for it in items:
        if it["published"] < cutoff:
            continue
        text = f"{it['title']} {it['abstract']}"
        if not KEYWORDS.search(text):
            continue
        key = re.sub(r"\W+", "", it["title"].lower())[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        kept.append(it)
    kept.sort(key=lambda x: x["published"], reverse=True)
    return kept[:MAX_ITEMS]


# ---------- 요약: Ollama ----------
def summarize(item):
    if not OLLAMA_ENABLE:
        return ""
    prompt = (
        "다음 논문/기사를 한국어로 3문장 이내로 핵심만 요약하라. "
        "전문 용어는 유지하되 간결하게. 불필요한 서론 없이 요약만 출력.\n\n"
        f"제목: {item['title']}\n\n초록/본문: {item['abstract'][:1500]}"
    )
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read())
        return resp.get("response", "").strip()
    except Exception as e:
        log(f"  Ollama 요약 실패: {e}")
        return ""


# ---------- HTML 생성 ----------
def build_post_html(items, date_str, seq=1):
    suffix = "" if seq == 1 else f" ({seq})"
    cards = []
    for it in items:
        summ = html.escape(it.get("summary_ko", "")) or "<em>요약 없음</em>"
        cards.append(f"""
    <article class="paper">
      <div class="paper-src">{html.escape(it['source'])}</div>
      <h2><a href="{html.escape(it['link'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a></h2>
      <p class="summary">{summ}</p>
    </article>""")

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_str}{suffix} · Photonics Digest</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#f5820b; }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;line-height:1.65}}
  header{{padding:40px 24px 20px;border-bottom:1px solid var(--border);text-align:center}}
  header h1{{font-size:1.8rem;letter-spacing:-.02em}}
  header h1 span{{color:var(--accent)}}
  header p{{color:var(--muted);margin-top:6px}}
  main{{max-width:820px;margin:0 auto;padding:28px 24px}}
  .paper{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:22px;margin-bottom:16px}}
  .paper:hover{{border-color:var(--accent)}}
  .paper-src{{color:var(--accent);font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}}
  .paper h2{{font-size:1.12rem;line-height:1.4;margin-bottom:10px}}
  .paper h2 a{{color:var(--text);text-decoration:none}}
  .paper h2 a:hover{{color:var(--accent);text-decoration:underline}}
  .summary{{color:var(--muted);font-size:.95rem}}
  .back{{display:inline-block;margin-bottom:20px;color:var(--accent);text-decoration:none}}
  footer{{text-align:center;padding:24px;color:var(--muted);font-size:.82rem;border-top:1px solid var(--border)}}
</style>
</head>
<body>
  <header>
    <h1>Photonics <span>Digest</span></h1>
    <p>{date_str}{suffix} · Si Photonics · PIC · Quantum Computing</p>
    <p style="font-size:.8rem;opacity:.7">Generated {datetime.now(KST):%Y-%m-%d %H:%M} KST</p>
  </header>
  <main>
    <a class="back" href="/">← Home</a>
    {"".join(cards) if cards else '<p style="color:var(--muted)">오늘은 새로운 항목이 없습니다.</p>'}
  </main>
  <footer>Auto-generated daily · arXiv + Phys.org + Nature Photonics · Ollama summaries</footer>
</body>
</html>
"""


def next_post_slot(posts_dir, date_str):
    """같은 날 재실행 시 덮어쓰지 않고 다음 회차 파일명을 반환.

    반환: (파일명, 회차)
      1회차 -> ("2026-07-09.html", 1)
      2회차 -> ("2026-07-09-2.html", 2)
      3회차 -> ("2026-07-09-3.html", 3) ...
    """
    first = f"{date_str}.html"
    if not os.path.exists(os.path.join(posts_dir, first)):
        return first, 1
    n = 2
    while True:
        name = f"{date_str}-{n}.html"
        if not os.path.exists(os.path.join(posts_dir, name)):
            return name, n
        n += 1


def update_index(repo_dir, filename, date_str, count, seq):
    """index.html 의 <!-- AUTO_CARDS --> 마커 바로 아래에 새 카드 삽입 (중복 방지)."""
    idx_path = os.path.join(repo_dir, "public", "index.html")
    with open(idx_path, encoding="utf-8") as f:
        content = f.read()

    href = f"/posts/{filename}"
    if href in content:  # 이미 있으면 스킵
        return
    label = f"{date_str} Digest" if seq == 1 else f"{date_str} Digest ({seq})"
    card = (f'\n      <a class="dl-card" href="{href}">\n'
            f'        <h3>{label}</h3>\n'
            f'        <p>{count}건 · Si Photonics · PIC · Quantum</p>\n'
            f'      </a>')
    marker = "<!-- AUTO_CARDS -->"
    if marker in content:
        content = content.replace(marker, marker + card, 1)
    else:  # 마커 없으면 cards div 열림 직후 삽입
        content = content.replace('<div class="cards">', '<div class="cards">\n      <!-- AUTO_CARDS -->' + card, 1)
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write(content)


# ---------- git push ----------
def git_push(repo_dir, date_str, seq=1):
    def run(*args):
        return subprocess.run(["git", "-C", repo_dir, *args],
                              check=True, capture_output=True, text=True)
    run("pull", "--quiet", GIT_REMOTE, GIT_BRANCH)
    run("add", "public/")
    # 변경 없으면 커밋 스킵
    status = subprocess.run(["git", "-C", repo_dir, "status", "--porcelain"],
                            capture_output=True, text=True).stdout.strip()
    if not status:
        log("변경 사항 없음 — 커밋/푸시 스킵")
        return False
    msg = f"Daily digest {date_str}" if seq == 1 else f"Daily digest {date_str} ({seq})"
    run("commit", "-m", msg)
    run("push", GIT_REMOTE, GIT_BRANCH)
    return True


# ---------- main ----------
def main():
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    log(f"=== Daily Photonics Digest {date_str} 시작 ===")

    raw = []
    for q in ARXIV_QUERIES:
        try:
            got = fetch_arxiv(q, ARXIV_MAX_PER_CAT)
            log(f"arXiv '{q[:40]}...' -> {len(got)}건")
            raw += got
            time.sleep(3)  # arXiv API 예의상 딜레이
        except Exception as e:
            log(f"arXiv 실패: {e}")
    for feed in RSS_FEEDS:
        got = fetch_rss(feed)
        log(f"RSS {_domain(feed)} -> {len(got)}건")
        raw += got

    items = filter_items(raw)
    log(f"필터 후 {len(items)}건 채택")
    if not items:
        log("채택 항목 없음 — 종료")
        return

    for i, it in enumerate(items, 1):
        log(f"[{i}/{len(items)}] 요약 중: {it['title'][:60]}")
        it["summary_ko"] = summarize(it)

    posts_dir = os.path.join(REPO_DIR, "public", "posts")
    os.makedirs(posts_dir, exist_ok=True)
    filename, seq = next_post_slot(posts_dir, date_str)
    with open(os.path.join(posts_dir, filename), "w", encoding="utf-8") as f:
        f.write(build_post_html(items, date_str, seq))
    update_index(REPO_DIR, filename, date_str, len(items), seq)
    log(f"HTML 생성 완료 -> posts/{filename} (회차 {seq})")

    if git_push(REPO_DIR, date_str, seq):
        log("git push 완료 — Cloudflare 자동 배포 대기")
    log("=== 종료 ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"치명적 오류: {e}")
        sys.exit(1)
