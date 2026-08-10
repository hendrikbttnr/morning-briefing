#!/usr/bin/env python3
"""
Morning Briefing – GitHub-Actions-Variante mit ICS-Abo-Kalender.

Vollstaendig in der GitHub-Cloud, kein eigener Rechner, KEIN Google-Login/Token noetig.
  1. Holt RSS-Feeds per HTTP (nicht erreichbare Quellen werden uebersprungen -> Fallback).
  2. Generiert ein HTML-Dashboard via Anthropic API.
  3. Schreibt es nach public/index.html -> GitHub Pages hostet es.
  4. Ergaenzt public/briefing.ics um einen Termin "Morning Briefing: DD/MM/YY" (08:00),
     mit Link zum Dashboard. Diese ICS abonnierst du EINMAL in Google Calendar per URL;
     neue Briefings erscheinen dann automatisch. Kein OAuth, kein Token.

Die ICS sammelt ALLE bisherigen Termine (wird ins Repo zurueckgeschrieben), damit ein
abonnierter Kalender die Historie nicht verliert.

Secrets: nur ANTHROPIC_API_KEY (aus GitHub-Actions-Secrets).
"""

import os
import re
import sys
import json
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ------------------------------------------------------------------ #
# Konfiguration
# ------------------------------------------------------------------ #
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL             = os.environ.get("BRIEFING_MODEL", "claude-sonnet-4-5").strip()
TZ_NAME           = os.environ.get("BRIEFING_TZ", "Europe/Berlin").strip()
PAGES_URL         = os.environ.get("PAGES_URL", "").strip()  # z. B. https://name.github.io/morning-briefing/

RSS_FEEDS = [
    ("FAZ Wirtschaft",        "https://www.faz.net/rss/aktuell/wirtschaft/"),
    ("Handelsblatt",          "https://www.handelsblatt.com/contentexport/feed/finanzen"),
    ("Reuters Business",      "https://www.reutersagency.com/feed/?best-topics=business-finance"),
    ("Tagesschau Wirtschaft", "https://www.tagesschau.de/wirtschaft/index~rss2.xml"),
]

PUBLIC_DIR = Path(__file__).parent / "public"
PUBLIC_DIR.mkdir(exist_ok=True)
ICS_PATH = PUBLIC_DIR / "briefing.ics"


def log(msg: str) -> None:
    stamp = dt.datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{stamp}] {msg}", flush=True)


def local_today() -> dt.date:
    return dt.datetime.now(ZoneInfo(TZ_NAME)).date()


def within_target_window() -> bool:
    if os.environ.get("FORCE_RUN", "").strip() == "1":
        return True
    now_local = dt.datetime.now(ZoneInfo(TZ_NAME))
    return now_local.hour == 8 or (now_local.hour == 7 and now_local.minute >= 40)


# ------------------------------------------------------------------ #
# 1. RSS sammeln
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


def write_public(html: str, today: dt.date) -> None:
    (PUBLIC_DIR / "index.html").write_text(html, encoding="utf-8")
    (PUBLIC_DIR / f"{today.strftime('%Y-%m-%d')}.html").write_text(html, encoding="utf-8")
    log("public/index.html geschrieben.")


# ------------------------------------------------------------------ #
# 3. ICS-Kalender pflegen (Termin ergaenzen, Historie erhalten)
# ------------------------------------------------------------------ #
def ics_escape(text: str) -> str:
    # RFC 5545: Kommas, Semikolons, Backslashes und Zeilenumbrueche maskieren.
    return (text.replace("\\", "\\\\")
                .replace(",", "\\,")
                .replace(";", "\\;")
                .replace("\n", "\\n"))


def build_event_block(today: dt.date) -> str:
    datum = today.strftime("%d/%m/%y")
    # Ganzjahres-stabile UID pro Tag -> kein Duplikat bei Mehrfachlauf.
    uid = f"briefing-{today.strftime('%Y%m%d')}@morning-briefing"
    dtstart = f"{today.strftime('%Y%m%d')}T080000"
    dtend   = f"{today.strftime('%Y%m%d')}T081500"
    dtstamp = dt.datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    link = PAGES_URL or "(kein Link gesetzt)"
    summary = ics_escape(f"Morning Briefing: {datum}")
    desc = ics_escape(f"Taegliches Dashboard: {link}")
    return "\n".join([
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID={TZ_NAME}:{dtstart}",
        f"DTEND;TZID={TZ_NAME}:{dtend}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{desc}",
        f"URL:{link}",
        "BEGIN:VALARM",
        "TRIGGER:PT0M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{summary}",
        "END:VALARM",
        "END:VEVENT",
    ])


def update_ics(today: dt.date) -> None:
    new_uid = f"briefing-{today.strftime('%Y%m%d')}@morning-briefing"
    new_block = build_event_block(today)

    header = "\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Morning Briefing//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Morning Briefing",
        "X-WR-TIMEZONE:" + TZ_NAME,
    ])
    footer = "END:VCALENDAR"

    existing_events = ""
    if ICS_PATH.exists():
        content = ICS_PATH.read_text(encoding="utf-8")
        # Bestehende VEVENT-Bloecke extrahieren
        blocks = re.findall(r"BEGIN:VEVENT.*?END:VEVENT", content, flags=re.DOTALL)
        # Heutigen (gleiche UID) rausfiltern, um Duplikate zu vermeiden
        blocks = [b for b in blocks if new_uid not in b]
        if blocks:
            existing_events = "\n".join(blocks) + "\n"

    ics = header + "\n" + existing_events + new_block + "\n" + footer + "\n"
    ICS_PATH.write_text(ics, encoding="utf-8")
    n = ics.count("BEGIN:VEVENT")
    log(f"briefing.ics aktualisiert ({n} Termin(e) gesamt).")


# ------------------------------------------------------------------ #
def main() -> None:
    if not within_target_window():
        log("Ausserhalb des 08:00-Zielfensters – Lauf uebersprungen.")
        return
    today = local_today()
    log(f"=== Morning Briefing {today.strftime('%d/%m/%y')} ===")
    feed_text = collect_feed_items()
    html = build_dashboard_html(feed_text, today)
    write_public(html, today)
    update_ics(today)
    log("=== Fertig ===")


if __name__ == "__main__":
    main()
