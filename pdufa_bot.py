"""
Alpha Breakout Lab — PDUFA Bot
================================
Scrapes SEC EDGAR and GlobeNewswire daily for new PDUFA date announcements.
Updates pdufa_calendar.html automatically and sends a digest email.

Safe by design:
- Never deletes existing entries
- Only ADDS new confirmed PDUFA dates
- Logs everything for debugging
- Sends daily summary regardless of findings
"""

import os
import re
import json
import logging
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config (set via GitHub Secrets / environment variables) ──────────────────
NOTIFY_EMAIL    = os.environ.get("NOTIFY_EMAIL", "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD", "")  # Gmail App Password
SMTP_FROM       = os.environ.get("NOTIFY_EMAIL", "")

# ── Month color map for new entries ──────────────────────────────────────────
MONTH_COLORS = {
    "JANUARY":   "#6D28D9", "FEBRUARY":  "#7C3AED", "MARCH":     "#8B5CF6",
    "APRIL":     "#0891B2", "MAY":       "#7C3AED", "JUNE":      "#0891B2",
    "JULY":      "#059669", "AUGUST":    "#D97706", "SEPTEMBER": "#BE185D",
    "OCTOBER":   "#DC2626", "NOVEMBER":  "#1D4ED8", "DECEMBER":  "#374151",
    "Q1 2026":   "#475569", "Q2 2026":   "#475569", "Q3 2026":   "#475569",
    "Q4 2026":   "#475569", "WATCHLIST": "#B45309",
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


def search_sec_edgar():
    """Search SEC EDGAR full-text search for recent PDUFA filings."""
    log.info("Searching SEC EDGAR for PDUFA filings...")
    findings = []

    try:
        # SEC EDGAR full text search API
        url = "https://efts.sec.gov/LATEST/search-index?q=%22PDUFA%22+%22target+action+date%22&dateRange=custom&startdt={}&enddt={}&forms=8-K".format(
            (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            datetime.now().strftime("%Y-%m-%d")
        )
        headers = {
            "User-Agent": "AlphaBreakoutLab pdufa-bot contact@alphabreakoutlab.com",
            "Accept": "application/json"
        }
        resp = requests.get(url, headers=headers, timeout=15)

        if resp.status_code != 200:
            log.warning(f"SEC EDGAR returned status {resp.status_code}")
            return findings

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])
        log.info(f"SEC EDGAR returned {len(hits)} filings")

        for hit in hits[:20]:  # limit to 20 most recent
            try:
                source = hit.get("_source", {})
                ticker = source.get("period_of_report", "")
                entity = source.get("entity_name", "Unknown")
                file_date = source.get("file_date", "")
                filing_url = source.get("file_num", "")
                accession = hit.get("_id", "").replace("-", "")

                # Get the actual filing text
                text_url = f"https://efts.sec.gov/LATEST/search-index?q=%22PDUFA%22&dateRange=custom&startdt={file_date}&enddt={file_date}&forms=8-K&entity={requests.utils.quote(entity)}"

                finding = {
                    "entity": entity,
                    "file_date": file_date,
                    "accession": accession,
                    "source": "SEC EDGAR 8-K"
                }

                # Try to extract PDUFA date from filing text snippet
                description = hit.get("_source", {}).get("period_of_report", "")
                highlights = hit.get("highlight", {})
                text_snippets = highlights.get("file_date", []) + highlights.get("period_of_report", [])

                pdufa_date = extract_date_from_text(" ".join(text_snippets))
                if pdufa_date:
                    finding["pdufa_date"] = pdufa_date

                findings.append(finding)
                log.info(f"Found filing: {entity} — {file_date}")

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
        # GlobeNewswire RSS feed for biotech/pharma PDUFA news
        url = "https://www.globenewswire.com/RssFeed/subjectcode/15-Drug%20Approvals/industry/Biotechnology"
        headers = {"User-Agent": "AlphaBreakoutLab pdufa-bot"}
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

                # Check if any PDUFA keyword is in title or description
                combined = (title + " " + description).lower()
                if not any(kw.lower() in combined for kw in PDUFA_KEYWORDS):
                    continue

                # Parse date
                try:
                    pub_date = datetime.strptime(pub_date_str[:25], "%a, %d %b %Y %H:%M:%S")
                    if pub_date < cutoff:
                        continue
                except:
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
                    "description": description[:300]
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

    # Pattern: Month DD, YYYY
    patterns = [
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(202[5-9])",
        r"(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(202[5-9])",
        r"(\d{1,2}/\d{1,2}/202[5-9])",
        r"(Q[1-4]\s+202[5-9])",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(0)

    return None


def extract_ticker_from_text(text):
    """Try to extract a stock ticker from text."""
    # Look for patterns like (NASDAQ: XXXX) or (NYSE: XXXX)
    match = re.search(r'\((?:NASDAQ|NYSE|OTCQB|OTC):\s*([A-Z]{2,5})\)', text)
    if match:
        return match.group(1)

    # Look for standalone tickers in caps
    match = re.search(r'\b([A-Z]{2,5})\b', text)
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
    except:
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

    entry = {
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
        "type": "standard"
    }

    return entry


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
    pattern = r'"ticker":\s*"([A-Z]{2,5})"'
    matches = re.findall(pattern, html_content)
    tickers.update(matches)
    log.info(f"Existing tickers in calendar: {tickers}")
    return tickers


def inject_new_entries(html_content, new_entries):
    """Safely inject new entries into the PDUFA_DATA array in the HTML."""
    if not new_entries:
        return html_content, 0

    # Find the PDUFA_DATA array
    marker = "const PDUFA_DATA = ["
    marker_pos = html_content.find(marker)
    if marker_pos == -1:
        log.error("Could not find PDUFA_DATA array in HTML file!")
        return html_content, 0

    # Find the closing bracket of the array
    # We insert new entries just before the closing ];
    close_marker = "];\n\n// ════"
    close_pos = html_content.find(close_marker, marker_pos)
    if close_pos == -1:
        # Try alternate closing pattern
        close_marker = "];\n\n//  TABLE"
        close_pos = html_content.find(close_marker, marker_pos)

    if close_pos == -1:
        log.error("Could not find end of PDUFA_DATA array!")
        return html_content, 0

    # Build new entry strings
    new_js_entries = []
    for entry in new_entries:
        js_entry = f"""  {{
    month: "{entry['month']}",
    monthColor: "{entry['monthColor']}",
    date: "{entry['date']}",
    isoDate: "{entry['isoDate']}",
    ticker: "{entry['ticker']}",
    company: "{entry['company'].replace('"', "'")}",
    drug: "{entry['drug']}",
    indication: "{entry['indication']}",
    cap: "{entry['cap']}",
    risk: "{entry['risk']}",
    notes: "{entry['notes'].replace('"', "'")}",
    type: "{entry['type']}"
  }}"""
        new_js_entries.append(js_entry)

    # Insert before closing bracket
    insertion = ",\n" + ",\n".join(new_js_entries) + "\n"
    new_html = html_content[:close_pos] + insertion + html_content[close_pos:]

    # Update the "last updated" date
    today = datetime.now().strftime("%B %d, %Y")
    new_html = re.sub(
        r'Updated \w+ \d+, \d{4}',
        f'Updated {today}',
        new_html
    )

    log.info(f"Injected {len(new_entries)} new entries into calendar")
    return new_html, len(new_entries)


def send_digest_email(findings, new_entries_count, errors):
    """Send daily digest email with results."""
    if not NOTIFY_EMAIL or not EMAIL_PASSWORD:
        log.warning("Email credentials not configured — skipping email")
        return

    today = datetime.now().strftime("%B %d, %Y")

    subject = f"🔬 Alpha Breakout Lab PDUFA Bot — {today} Digest"

    if new_entries_count > 0:
        subject = f"🚀 {new_entries_count} NEW PDUFA Date(s) Found! — {today}"

    body = f"""
<html><body style="font-family: Arial, sans-serif; background: #0F1420; color: #E2E8F0; padding: 24px;">

<h1 style="color: #7C3AED; font-size: 24px;">Alpha Breakout Lab</h1>
<h2 style="color: #06B6D4;">PDUFA Bot Daily Digest — {today}</h2>

<div style="background: #161C2E; border: 1px solid #1E2640; border-radius: 8px; padding: 20px; margin: 16px 0;">
  <h3 style="color: #10B981;">📊 Summary</h3>
  <p>SEC EDGAR filings scanned: <strong>{len([f for f in findings if f.get('source') == 'SEC EDGAR 8-K'])}</strong></p>
  <p>GlobeNewswire items scanned: <strong>{len([f for f in findings if f.get('source') == 'GlobeNewswire'])}</strong></p>
  <p>New entries added to calendar: <strong style="color: {'#10B981' if new_entries_count > 0 else '#64748B'};">{new_entries_count}</strong></p>
  <p>Errors encountered: <strong style="color: {'#EF4444' if errors else '#10B981'};">{len(errors)}</strong></p>
</div>

{"".join([f'''
<div style="background: #161C2E; border: 1px solid #7C3AED; border-radius: 8px; padding: 16px; margin: 12px 0;">
  <h4 style="color: #7C3AED; margin: 0 0 8px;">🆕 NEW: {f.get('ticker', 'TBD')} — {f.get('entity', 'Unknown')[:60]}</h4>
  <p style="color: #94A3B8; font-size: 13px;">PDUFA Date: <strong style="color: #06B6D4;">{f.get('pdufa_date', 'TBD')}</strong></p>
  <p style="color: #94A3B8; font-size: 12px;">Source: {f.get('source', 'Unknown')}</p>
  {"<p style='color: #94A3B8; font-size: 12px;'>Link: <a href='" + f.get('link','') + "' style='color: #7C3AED;'>View filing</a></p>" if f.get('link') else ''}
  <p style="color: #64748B; font-size: 11px; font-style: italic;">⚠️ Auto-detected — please verify details before publishing</p>
</div>
''' for f in findings if f.get('added')]) if new_entries_count > 0 else '<p style="color: #64748B;">No new PDUFA dates detected today.</p>'}

{"".join([f'<p style="color: #EF4444; font-size: 12px;">⚠️ Error: {e}</p>' for e in errors]) if errors else ''}

<div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #1E2640;">
  <p style="color: #64748B; font-size: 11px;">
    This is an automated message from Alpha Breakout Lab PDUFA Bot.<br>
    All auto-detected entries should be manually verified before relying on them for trading decisions.<br>
    View your live calendar at <a href="https://alphabreakoutlab.com/pdufa_calendar.html" style="color: #7C3AED;">alphabreakoutlab.com/pdufa_calendar.html</a>
  </p>
</div>

</body></html>
"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_FROM, EMAIL_PASSWORD)
            server.sendmail(SMTP_FROM, NOTIFY_EMAIL, msg.as_string())

        log.info(f"Digest email sent to {NOTIFY_EMAIL}")

    except Exception as e:
        log.error(f"Failed to send email: {e}")


def main():
    log.info("=" * 60)
    log.info("Alpha Breakout Lab PDUFA Bot Starting")
    log.info(f"Run date: {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    errors = []
    all_findings = []
    entries_added = 0

    # ── Read current calendar ─────────────────────────────────────────────────
    html_content = read_current_calendar("pdufa_calendar.html")
    if not html_content:
        log.error("Cannot read calendar file — aborting")
        return

    existing_tickers = extract_existing_tickers(html_content)

    # ── Search SEC EDGAR ──────────────────────────────────────────────────────
    try:
        sec_findings = search_sec_edgar()
        all_findings.extend(sec_findings)
    except Exception as e:
        error_msg = f"SEC EDGAR search failed: {e}"
        log.error(error_msg)
        errors.append(error_msg)

    # ── Search GlobeNewswire ──────────────────────────────────────────────────
    try:
        gnw_findings = search_globenewswire()
        all_findings.extend(gnw_findings)
    except Exception as e:
        error_msg = f"GlobeNewswire search failed: {e}"
        log.error(error_msg)
        errors.append(error_msg)

    log.info(f"Total findings before dedup: {len(all_findings)}")

    # ── Filter new entries (not already in calendar) ──────────────────────────
    new_entries = []
    for finding in all_findings:
        ticker = finding.get("ticker", "")
        if not ticker or ticker in existing_tickers or ticker == "TBD":
            continue
        if not finding.get("pdufa_date"):
            continue

        entry = build_new_entry(finding)
        new_entries.append(entry)
        finding["added"] = True
        log.info(f"New entry queued: {ticker} — {finding.get('pdufa_date')}")

    # ── Inject new entries into calendar ─────────────────────────────────────
    if new_entries:
        updated_html, entries_added = inject_new_entries(html_content, new_entries)
        if entries_added > 0:
            with open("pdufa_calendar.html", "w", encoding="utf-8") as f:
                f.write(updated_html)
            log.info(f"Calendar updated with {entries_added} new entries")
    else:
        log.info("No new entries to add — calendar unchanged")

    # ── Send digest email ─────────────────────────────────────────────────────
    send_digest_email(all_findings, entries_added, errors)

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"Bot run complete. New entries added: {entries_added}")
    log.info(f"Errors: {len(errors)}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
