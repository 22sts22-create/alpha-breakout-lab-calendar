"""
Alpha Breakout Lab — PDUFA Bot  (FIXED)
========================================
Scrapes SEC EDGAR and GlobeNewswire daily for new PDUFA date announcements.
Updates pdufa_calendar.html automatically and sends a Pushover notification.

Safe by design:
- Never deletes existing entries
- Only ADDS new confirmed PDUFA dates
- Logs everything for debugging
- Sends daily summary regardless of findings

FIXES vs. previous version:
  1. SEC EDGAR endpoint now called with correct params (q / forms / startdt /
     enddt), no malformed f-string leaking `{...}` into the URL.
  2. Parses the REAL _source envelope: display_names, file_date, form, _id (adsh).
  3. Ticker + PDUFA date extracted from the highlight text that actually exists.
  4. HTML injection anchors on the PDUFA_DATA array bracket robustly (regex),
     not on exact box-drawing whitespace.
"""

import os
import re
import json
import time
import logging
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config (set via GitHub Secrets / environment variables) ──────────────────
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY", "")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN", "")

# SEC fair-access policy asks for an identifying UA + contact email.
SEC_HEADERS = {
    "User-Agent": "AlphaBreakoutLab pdufa-bot contact@alphabreakoutlab.com",
    "Accept": "application/json",
}

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"  # NOTE: /LATEST/ is uppercase

# ── Month color map for new entries ──────────────────────────────────────────
MONTH_COLORS = {
    "JANUARY": "#6D28D9", "FEBRUARY": "#7C3AED", "MARCH": "#8B5CF6",
    "APRIL": "#0891B2", "MAY": "#7C3AED", "JUNE": "#0891B2",
    "JULY": "#059669", "AUGUST": "#D97706", "SEPTEMBER": "#BE185D",
    "OCTOBER": "#DC2626", "NOVEMBER": "#1D4ED8", "DECEMBER": "#374151",
    "Q1 2026": "#475569", "Q2 2026": "#475569", "Q3 2026": "#475569",
    "Q4 2026": "#475569", "WATCHLIST": "#B45309",
}

# ── Keywords that indicate a PDUFA date announcement ─────────────────────────
PDUFA_KEYWORDS = [
    "PDUFA", "prescription drug user fee", "target action date",
    "NDA accepted", "BLA accepted", "NDA acceptance", "BLA acceptance",
    "FDA accepts", "FDA accepted", "PDUFA date", "target date"
]

# ── Month name to number ──────────────────────────────────────────────────────
MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12"
}


def fetch_filing_text(cik, accession, doc_id):
    """Fetch the primary document text for an EDGAR filing.

    The Archives path REQUIRES the filer CIK (leading zeros stripped):
        https://www.sec.gov/Archives/edgar/data/<CIK_int>/<acc_nodash>/<filename>
    cik:       from _source.ciks[0] (e.g. "0001114036")
    accession: "0001234567-25-000123"
    doc_id:    "0001234567-25-000123:primary-doc.htm"
    Returns plain text (tags stripped), or "".
    """
    try:
        filename = doc_id.split(":", 1)[1] if ":" in doc_id else ""
        if not (cik and accession and filename):
            return ""
        cik_int = str(int(cik))               # strip leading zeros
        acc_nodash = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{filename}"
        resp = requests.get(url, headers=SEC_HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"Filing fetch {resp.status_code} for {accession} ({url})")
            return ""
        text = BeautifulSoup(resp.content, "html.parser").get_text(" ", strip=True)
        time.sleep(0.15)
        return text
    except Exception as e:
        log.warning(f"Could not fetch filing {accession}: {e}")
        return ""


def extract_pdufa_date_from_filing(text):
    """Find a date that appears NEAR PDUFA language, not just any date in the text.

    A filing has many dates; we want the one tied to 'PDUFA date',
    'target action date', 'goal date', etc. Search a window around those phrases.
    """
    if not text:
        return None
    cues = [
        "pdufa date", "pdufa goal date", "target action date",
        "goal date", "action date", "prescription drug user fee",
    ]
    low = text.lower()
    for cue in cues:
        idx = low.find(cue)
        while idx != -1:
            window = text[idx: idx + 160]  # look just after the cue phrase
            date = extract_date_from_text(window)
            if date:
                return date
            idx = low.find(cue, idx + 1)
    # Fallback: any qualifying date anywhere in the doc.
    return extract_date_from_text(text)


def search_sec_edgar():
    """Search SEC EDGAR full-text search for recent PDUFA filings.

    Uses the real EFTS endpoint contract:
      GET https://efts.sec.gov/LATEST/search-index
          ?q="PDUFA" ?forms=8-K &dateRange=custom &startdt=... &enddt=...
    Response is Elasticsearch JSON: hits.hits[]._source has
    display_names (["Company Inc. (TICK)"]), file_date, form; _id is "adsh:file".
    """
    log.info("Searching SEC EDGAR for PDUFA filings...")
    findings = []

    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")

    params = {
        "q": '"PDUFA" "target action date"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start,
        "enddt": end,
    }

    try:
        resp = requests.get(EFTS_URL, params=params, headers=SEC_HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"SEC EDGAR returned status {resp.status_code}: {resp.text[:200]}")
            return findings

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        log.info(f"SEC EDGAR returned {len(hits)} filings")

        for hit in hits[:20]:  # limit to 20 most recent
            try:
                source = hit.get("_source", {})

                # display_names looks like ["Cingulate Inc. (CING) (CIK ...)"]
                display_names = source.get("display_names", [])
                display = display_names[0] if display_names else "Unknown"

                entity = re.sub(r"\s*\(.*$", "", display).strip() or "Unknown"
                ticker = extract_ticker_from_text(display)

                file_date = source.get("file_date", "")
                form = source.get("form", "8-K")

                # accession id: "0001234567-25-000123:primary.htm"
                accession = hit.get("_id", "").split(":")[0]

                # CIK is needed to build the Archives URL. It's in _source.ciks
                # (list of zero-padded strings). Fall back to display_names' CIK.
                ciks = source.get("ciks", [])
                cik = ciks[0] if ciks else ""
                if not cik:
                    m = re.search(r"CIK\s*0*(\d+)", display)
                    cik = m.group(1) if m else ""

                # Highlighted snippets are where the matched phrase lives.
                highlights = hit.get("highlight", {})
                snippet_bits = []
                for v in highlights.values():
                    if isinstance(v, list):
                        snippet_bits.extend(v)
                    elif isinstance(v, str):
                        snippet_bits.append(v)
                snippet = " ".join(snippet_bits)

                # Strip <em> highlight tags EDGAR wraps around matches.
                snippet = re.sub(r"</?em>", "", snippet)

                pdufa_date = extract_date_from_text(snippet) or extract_date_from_text(display)

                # The highlight snippet is a short fragment and often does NOT
                # contain the actual PDUFA date (it's in the filing body). If we
                # didn't get a date, fetch the filing text and search that.
                if not pdufa_date and accession and cik:
                    filing_text = fetch_filing_text(cik, accession, hit.get("_id", ""))
                    pdufa_date = extract_pdufa_date_from_filing(filing_text)
                    if filing_text and not snippet:
                        snippet = filing_text[:300]

                finding = {
                    "entity": entity,
                    "ticker": ticker,
                    "file_date": file_date,
                    "accession": accession,
                    "pdufa_date": pdufa_date,
                    "description": snippet[:300],
                    "source": f"SEC EDGAR {form}",
                }
                findings.append(finding)
                log.info(f"Found filing: {entity} ({ticker}) — {file_date} — PDUFA {pdufa_date}")

                time.sleep(0.15)  # stay under SEC's ~10 req/s courtesy limit
            except Exception as e:
                log.warning(f"Error parsing filing: {e}")
                continue

    except Exception as e:
        log.error(f"SEC EDGAR search failed: {e}")

    return findings


def search_globenewswire():
    """Search GlobeNewswire RSS for PDUFA announcements."""
    log.info("Searching GlobeNewswire for PDUFA announcements...")
    findings = []
    try:
        url = "https://www.globenewswire.com/RssFeed/subjectcode/15-Drug%20Approvals/industry/Biotechnology"
        # GlobeNewswire returns 400 for a bare bot UA — send a browser-like set.
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0 Safari/537.36"),
            "Accept": "application/rss+xml, application/xml, text/xml; q=0.9, */*; q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            log.warning(f"GlobeNewswire returned status {resp.status_code}")
            return findings

        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")
        log.info(f"GlobeNewswire returned {len(items)} items")

        cutoff = datetime.now() - timedelta(days=7)
        for item in items:
            try:
                title = item.find("title").text if item.find("title") else ""
                description = item.find("description").text if item.find("description") else ""
                pub_date_str = item.find("pubDate").text if item.find("pubDate") else ""
                link = item.find("link").text if item.find("link") else ""

                combined = (title + " " + description).lower()
                if not any(kw.lower() in combined for kw in PDUFA_KEYWORDS):
                    continue

                try:
                    pub_date = datetime.strptime(pub_date_str[:25], "%a, %d %b %Y %H:%M:%S")
                    if pub_date < cutoff:
                        continue
                except Exception:
                    pass

                pdufa_date = extract_date_from_text(title + " " + description)
                ticker = extract_ticker_from_text(title + " " + description)

                finding = {
                    "entity": title[:80],
                    "ticker": ticker,
                    "pdufa_date": pdufa_date,
                    "link": link,
                    "pub_date": pub_date_str,
                    "source": "GlobeNewswire",
                    "description": description[:300],
                }
                findings.append(finding)
                log.info(f"GlobeNewswire: {title[:60]}")
            except Exception as e:
                log.warning(f"Error parsing GlobeNewswire item: {e}")
                continue

    except Exception as e:
        log.error(f"GlobeNewswire search failed: {e}")

    return findings


def extract_date_from_text(text):
    """Extract a PDUFA date from text using regex patterns."""
    if not text:
        return None
    patterns = [
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(202[5-9])",
        r"(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(202[5-9])",
        # "5 December 2026" ordering (less common but appears in some PRs)
        r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(202[5-9])",
        r"(\d{1,2}/\d{1,2}/202[5-9])",
        r"(Q[1-4]\s+202[5-9])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def extract_ticker_from_text(text):
    """Try to extract a stock ticker from text.

    Handles EDGAR display_names format "Company Inc. (CING) (CIK ...)" plus the
    press-release "(NASDAQ: XXXX)" style.
    """
    if not text:
        return "TBD"

    # display_names style: first (XXXX) that isn't a CIK
    for m in re.finditer(r"\(([A-Z]{1,5})\)", text):
        cand = m.group(1)
        if cand != "CIK":
            return cand

    # (NASDAQ: XXXX) / (NYSE: XXXX) style
    match = re.search(r'\((?:NASDAQ|NYSE|OTCQB|OTC|AMEX):\s*([A-Z]{1,5})\)', text)
    if match:
        return match.group(1)

    return "TBD"


def get_month_from_date(date_str):
    """Get month label from date string."""
    if not date_str:
        return "WATCHLIST"
    date_upper = date_str.upper()
    for month_name in MONTH_MAP.keys():
        if month_name.upper() in date_upper:
            return month_name.upper()
    for q in ["Q1", "Q2", "Q3", "Q4"]:
        if q in date_upper:
            year_match = re.search(r'202\d', date_upper)
            year = year_match.group(0) if year_match else "2026"
            return f"{q} {year}"
    return "WATCHLIST"


def format_iso_date(date_str):
    """Try to create an ISO date string."""
    if not date_str:
        return "2026-12-31"
    try:
        for month_name, month_num in MONTH_MAP.items():
            if month_name in date_str.lower():
                day_match = re.search(r'\b(\d{1,2})\b', date_str)
                year_match = re.search(r'(202\d)', date_str)
                if day_match and year_match:
                    day = day_match.group(1).zfill(2)
                    year = year_match.group(1)
                    return f"{year}-{month_num}-{day}"
    except Exception:
        pass
    return "2026-12-31"


def build_new_entry(finding):
    """Build a new PDUFA_DATA entry from a finding."""
    ticker = finding.get("ticker", "TBD")
    entity = finding.get("entity", "Unknown Company")
    pdufa_date = finding.get("pdufa_date", "TBD")
    month = get_month_from_date(pdufa_date)
    month_color = MONTH_COLORS.get(month, "#475569")
    iso_date = format_iso_date(pdufa_date)
    source = finding.get("source", "SEC Filing")
    description = finding.get("description", "")[:200]

    return {
        "month": month,
        "monthColor": month_color,
        "date": pdufa_date if pdufa_date else "TBD",
        "isoDate": iso_date,
        "ticker": ticker,
        "company": entity,
        "drug": "See filing for details",
        "indication": "See filing for details",
        "cap": "Unknown",
        "risk": "HIGH",
        "notes": f"Auto-detected from {source}. Verify details in SEC filing. {description}",
        "type": "standard",
    }


def read_current_calendar(filepath="pdufa_calendar.html"):
    """Read the current HTML calendar file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        log.error(f"Calendar file not found: {filepath}")
        return None


def extract_existing_tickers(html_content):
    """Extract tickers already in the calendar to avoid duplicates."""
    tickers = set()
    pattern = r'ticker:\s*"([A-Z]{1,5})"'   # matches JS object literal in the file
    tickers.update(re.findall(pattern, html_content))
    # also tolerate JSON-style "ticker": "XXXX"
    tickers.update(re.findall(r'"ticker":\s*"([A-Z]{1,5})"', html_content))
    log.info(f"Existing tickers in calendar: {sorted(tickers)}")
    return tickers


def inject_new_entries(html_content, new_entries):
    """Safely inject new entries into the PDUFA_DATA array in the HTML.

    Robust anchor: find `const PDUFA_DATA = [` then match forward to the FIRST
    `];` that closes it. Insert new entries just before that `];`. Does not rely
    on exact whitespace or box-drawing comment characters.
    """
    if not new_entries:
        return html_content, 0

    marker = "const PDUFA_DATA = ["
    marker_pos = html_content.find(marker)
    if marker_pos == -1:
        log.error("Could not find PDUFA_DATA array in HTML file!")
        return html_content, 0

    # Find the closing `];` for the array by scanning bracket depth from the [.
    array_open = html_content.find("[", marker_pos)
    depth = 0
    close_pos = -1
    i = array_open
    while i < len(html_content):
        ch = html_content[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                close_pos = i  # index of the closing ]
                break
        i += 1

    if close_pos == -1:
        log.error("Could not find end of PDUFA_DATA array!")
        return html_content, 0

    # Build new entry strings.
    new_js_entries = []
    for entry in new_entries:
        company = entry["company"].replace('"', "'")
        notes = entry["notes"].replace('"', "'")
        js_entry = f"""  {{
    month: "{entry['month']}",
    monthColor: "{entry['monthColor']}",
    date: "{entry['date']}",
    isoDate: "{entry['isoDate']}",
    ticker: "{entry['ticker']}",
    company: "{company}",
    drug: "{entry['drug']}",
    indication: "{entry['indication']}",
    cap: "{entry['cap']}",
    risk: "{entry['risk']}",
    notes: "{notes}",
    type: "{entry['type']}"
  }}"""
        new_js_entries.append(js_entry)

    # Ensure the element before `]` ends with a comma, then add our entries.
    before = html_content[:close_pos].rstrip()
    if not before.endswith(",") and not before.endswith("["):
        before += ","
    insertion = "\n" + ",\n".join(new_js_entries) + "\n"
    new_html = before + insertion + html_content[close_pos:]

    # Update the "last updated" date badge.
    today = datetime.now().strftime("%B %d, %Y")
    new_html = re.sub(r'Updated \w+ \d+, \d{4}', f'Updated {today}', new_html)

    log.info(f"Injected {len(new_entries)} new entries into calendar")
    return new_html, len(new_entries)


def send_pushover_notification(findings, new_entries_count, errors):
    """Send Pushover push notification with daily digest."""
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        log.warning("Pushover credentials not configured — skipping notification")
        return

    today = datetime.now().strftime("%b %d, %Y")

    if new_entries_count > 0:
        title = f"🚀 {new_entries_count} NEW PDUFA Date(s) Found!"
        new_tickers = [f.get('ticker', '?') for f in findings if f.get('added')]
        message = f"<b>Alpha Breakout Lab PDUFA Bot</b> — {today}\n\n"
        message += f"<b>New entries added:</b> {new_entries_count}\n"
        message += f"<b>Tickers:</b> {', '.join(new_tickers)}\n\n"
        for f in findings:
            if f.get('added'):
                message += f"• <b>{f.get('ticker','?')}</b> — {f.get('pdufa_date','TBD')} ({f.get('source','')})\n"
        message += f"\n⚠️ Auto-detected — verify before trading!"
        priority = 1
    else:
        title = f"✅ PDUFA Bot — Daily Check Complete"
        message = f"<b>Alpha Breakout Lab PDUFA Bot</b> — {today}\n\n"
        message += f"No new PDUFA dates detected today.\n"
        message += f"Sources checked: SEC EDGAR + GlobeNewswire\n"
        message += f"Errors: {len(errors)}"
        if errors:
            message += f"\n\n⚠️ Errors:\n" + "\n".join(errors[:3])
        priority = -1

    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "html": 1,
                "priority": priority,
                "sound": "cashregister" if new_entries_count > 0 else "none",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("Pushover notification sent successfully!")
        else:
            log.error(f"Pushover failed: {resp.status_code} — {resp.text}")
    except Exception as e:
        log.error(f"Pushover notification failed: {e}")


def main():
    log.info("=" * 60)
    log.info("Alpha Breakout Lab PDUFA Bot Starting")
    log.info(f"Run date: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    errors = []
    all_findings = []
    entries_added = 0

    html_content = read_current_calendar("pdufa_calendar.html")
    if not html_content:
        log.error("Cannot read calendar file — aborting")
        return

    existing_tickers = extract_existing_tickers(html_content)

    try:
        all_findings.extend(search_sec_edgar())
    except Exception as e:
        msg = f"SEC EDGAR search failed: {e}"
        log.error(msg)
        errors.append(msg)

    try:
        all_findings.extend(search_globenewswire())
    except Exception as e:
        msg = f"GlobeNewswire search failed: {e}"
        log.error(msg)
        errors.append(msg)

    log.info(f"Total findings before dedup: {len(all_findings)}")

    new_entries = []
    seen_this_run = set()
    for finding in all_findings:
        ticker = finding.get("ticker", "")
        if not ticker or ticker in existing_tickers or ticker == "TBD":
            continue
        if ticker in seen_this_run:      # dedupe within this run too
            continue
        if not finding.get("pdufa_date"):
            continue
        new_entries.append(build_new_entry(finding))
        finding["added"] = True
        seen_this_run.add(ticker)
        log.info(f"New entry queued: {ticker} — {finding.get('pdufa_date')}")

    if new_entries:
        updated_html, entries_added = inject_new_entries(html_content, new_entries)
        if entries_added > 0:
            with open("pdufa_calendar.html", "w", encoding="utf-8") as f:
                f.write(updated_html)
            log.info(f"Calendar updated with {entries_added} new entries")
    else:
        log.info("No new entries to add — calendar unchanged")

    send_pushover_notification(all_findings, entries_added, errors)

    log.info("=" * 60)
    log.info(f"Bot run complete. New entries added: {entries_added}")
    log.info(f"Errors: {len(errors)}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
