#!/usr/bin/env python3
"""
Morning Briefing – GitHub-Actions-Variante.

Laeuft vollstaendig in der GitHub-Cloud, kein eigener Rechner noetig.
  1. Holt RSS-Feeds per HTTP (nicht erreichbare Quellen werden uebersprungen -> Fallback).
  2. Generiert ein HTML-Dashboard via Anthropic API.
  3. Schreibt es nach public/index.html -> wird von GitHub Pages gehostet.
  4. Legt einen Google-Calendar-Eintrag "Morning Briefing: DD/MM/YY" um 08:00 an.

Alle Secrets kommen aus den GitHub-Actions-Secrets (Umgebungsvariablen), NICHT aus .env.
"""

import os
import sys
import json
import base64
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------ #
# Konfiguration aus Umgebungsvariablen (GitHub Secrets)
# ------------------------------------------------------------------ #
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL             = os.environ.get("BRIEFING_MODEL", "claude-sonnet-4-5").strip()
TZ_NAME           = os.environ.get("BRIEFING_TZ", "Europe/Berlin").strip()

# Google OAuth (als Secrets hinterlegt, siehe Anleitung)
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "").strip()  # Inhalt credentials.json
GOOGLE_TOKEN       = os.environ.get("GOOGLE_TOKEN", "").strip()        # Inhalt token.json

# Oeffentliche Pages-URL (fuer den Link im Kalender). Wird als Secret/Variable gesetzt.
PAGES_URL = os.environ.get("PAGES_URL", "").strip()

RSS_FEEDS = [
    ("FAZ Wirtschaft",        "https://www.faz.net/rss/aktuell/wirtschaft/"),
    ("Handelsblatt",          "https://www.handelsblatt.com/contentexport/feed/finanzen"),
    ("Reuters Business",      "https://www.reutersagency.com/feed/?best-topics=business-finance"),
    ("Tagesschau Wirtschaft", "https://www.tagesschau.de/wirtschaft/index~rss2.xml"),
]

PUBLIC_DIR = Path(__file__).parent / "public"
PUBLIC_DIR.mkdir(exist_ok=True)


def log(msg: str) -> None:
    stamp = dt.datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {msg}", flush=True)


def local_today() -> dt.date:
    return dt.datetime.now(ZoneInfo(TZ_NAME)).date()


# ------------------------------------------------------------------ #
# Zeitfenster-Check: Actions-cron laeuft in UTC und ungenau.
# Wir feuern per cron etwas grosszuegig und lassen NUR den Lauf durch,
# der zur lokalen 08:00-Stunde passt. So klappt es Sommer wie Winter.
# ------------------------------------------------------------------ #
def within_target_window() -> bool:
    # Manuelle Ausloesung (workflow_dispatch) immer erlauben.
    if os.environ.get("FORCE_RUN", "").strip() == "1":
        return True
    now_local = dt.datetime.now(ZoneInfo(TZ_NAME))
    # Zielstunde 8; Toleranz, da Actions bis ~20 Min spaeter startet.
    return now_local.hour == 8 or (now_local.hour == 7 and now_local.minute >= 40)


# ------------------------------------------------------------------ #
# 1. RSS sammeln (Fallback pro Feed)
# ------------------------------------------------------------------ #
def collect_feed_items(max_per_feed: int = 6) -> str:
    import xml.etree.ElementTree as ET
    chunks = []
    for name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=12, headers={"User-Agent": "MorningBriefing/1.0"})
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            if not items:
                ns = {"a": "http://www.w3.org/2005/Atom"}
                items = root.findall(".//a:entry", ns)
            lines = []
            for it in items[:max_per_feed]:
                title = (it.findtext("title") or "").strip()
                if not title:
                    t = it.find("{http://www.w3.org/2005/Atom}title")
                    title = (t.text or "").strip() if t is not None else ""
                if title:
                    lines.append(f"  - {title}")
            if lines:
                chunks.append(f"### {name}\n" + "\n".join(lines))
                log(f"Feed OK: {name} ({len(lines)})")
            else:
                log(f"Feed leer: {name}")
        except Exception as e:
            log(f"Feed uebersprungen ({name}): {e}")
            continue
    return "\n\n".join(chunks) if chunks else "(Keine RSS-Quellen erreichbar.)"


# ------------------------------------------------------------------ #
# 2. Dashboard generieren
# ------------------------------------------------------------------ #
def build_dashboard_html(feed_text: str, today: dt.date) -> str:
    if not ANTHROPIC_API_KEY:
        log("FEHLER: ANTHROPIC_API_KEY fehlt."); sys.exit(1)

    datum = today.strftime("%d/%m/%y")
    system = (
        "Du bist ein Analyst, der ein taegliches Morning-Briefing-Dashboard als eigenstaendige "
        "HTML-Datei erstellt. Gib AUSSCHLIESSLICH vollstaendigen HTML-Code aus, beginnend mit "
        "<!DOCTYPE html> und endend mit </html>. Kein Markdown, keine Backticks, keine Erklaerung. "
        "Self-contained (Inline-CSS), responsive fuer Desktop und Handy, klare Hierarchie: "
        "Uebersichtskarten oben, dann thematisch gruppierte News. Sauberes, professionelles Layout."
    )
    user = (
        f"Erstelle das Morning-Briefing-Dashboard fuer den {datum}.\n\n"
        f"Nutze diese aktuellen Schlagzeilen, fasse zusammen, gruppiere thematisch "
        f"(Makro/Zinsen, Immobilien, Maerkte, Sonstiges) und hebe fuer Real-Estate-Capital-"
        f"Markets Relevantes hervor:\n\n{feed_text}\n\n"
        f"Titel oben: 'Morning Briefing - {datum}'. Fehlende Quellen einfach weglassen."
    )
    payload = {
        "model": MODEL, "max_tokens": 8000, "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    log(f"Anthropic API ({MODEL}) ...")
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        data=json.dumps(payload), timeout=120,
    )
    if r.status_code != 200:
        log(f"API-Fehler {r.status_code}: {r.text[:400]}"); sys.exit(1)
    data = r.json()
    html = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip()
    if html.startswith("```"):
        html = html.strip("`").replace("html\n", "", 1).strip()
    if "<html" not in html.lower():
        html = (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                f"<title>Morning Briefing {datum}</title></head><body>"
                f"<h1>Morning Briefing {datum}</h1><pre>{feed_text}</pre></body></html>")
    log(f"Dashboard generiert ({len(html)} Zeichen).")
    return html


# ------------------------------------------------------------------ #
# 3. Nach public/index.html schreiben (GitHub Pages hostet das)
# ------------------------------------------------------------------ #
def write_public(html: str, today: dt.date) -> None:
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")
    # zusaetzlich datierte Archivkopie
    (PUBLIC_DIR / f"{today.strftime('%Y-%m-%d')}.html").write_text(html, encoding="utf-8")
    log("public/index.html geschrieben.")


# ------------------------------------------------------------------ #
# 4. Google-Calendar-Eintrag
# ------------------------------------------------------------------ #
def create_calendar_event(today: dt.date) -> None:
    if not GOOGLE_TOKEN:
        log("GOOGLE_TOKEN fehlt – Kalendereintrag uebersprungen.")
        return
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        log("Google-Bibliotheken fehlen – Kalendereintrag uebersprungen.")
        return

    SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
    info = json.loads(GOOGLE_TOKEN)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    service = build("calendar", "v3", credentials=creds)
    datum = today.strftime("%d/%m/%y")
    start = dt.datetime.combine(today, dt.time(8, 0))
    end   = start + dt.timedelta(minutes=15)
    link = PAGES_URL or "(kein Link gesetzt)"

    event = {
        "summary": f"Morning Briefing: {datum}",
        "description": f"Taegliches Dashboard:\n{link}",
        "start": {"dateTime": start.isoformat(), "timeZone": TZ_NAME},
        "end":   {"dateTime": end.isoformat(),   "timeZone": TZ_NAME},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
    }
    try:
        created = service.events().insert(calendarId="primary", body=event).execute()
        log(f"Kalendereintrag erstellt: {created.get('htmlLink')}")
    except Exception as e:
        log(f"Kalendereintrag fehlgeschlagen: {e}")


def main() -> None:
    if not within_target_window():
        log("Ausserhalb des 08:00-Zielfensters – Lauf uebersprungen "
            "(Actions-cron feuert breiter, das ist normal).")
        return
    today = local_today()
    log(f"=== Morning Briefing {today.strftime('%d/%m/%y')} ===")
    feed_text = collect_feed_items()
    html = build_dashboard_html(feed_text, today)
    write_public(html, today)
    create_calendar_event(today)
    log("=== Fertig ===")


if __name__ == "__main__":
    main()
