#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecte des flux RSS + interrogation d'Altmetric, cote serveur.

Lance par GitHub Actions plusieurs fois par jour. Ecrit data/feed.json,
que la page index.html lit ensuite directement (meme origine, aucun proxy).

Le fichier est CUMULATIF : les articles deja vus sont conserves meme
lorsqu'ils disparaissent du flux RSS d'origine.
"""

import datetime as dt
import json
import pathlib
import re
import sys
import time

import feedparser
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.json"
OUT_FILE = ROOT / "data" / "feed.json"

RETENTION_DAYS = 120        # on oublie les articles plus vieux que ca
MAX_ITEMS = 4000            # plafond de securite sur la taille du fichier
ALT_RECHECK_HOURS = 24      # delai minimal avant de re-interroger Altmetric
ALT_FRESH_DAYS = 45         # au-dela, on ne re-interroge plus (couverture figee)
ALT_PAUSE = 1.1             # secondes entre deux appels Altmetric (politesse)
ALT_MAX_CALLS = 250         # plafond d'appels Altmetric par execution

UA = "VeilleScientifique/1.0 (+https://github.com/)"
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>&\]\)]+")
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def clean_text(raw):
    """Retire le HTML, normalise les espaces, coupe si trop long."""
    if not raw:
        return ""
    txt = WS_RE.sub(" ", TAG_RE.sub(" ", raw)).strip()
    if len(txt) < 40 or txt.lower().startswith("http"):
        return ""
    return txt[:1200] + ("\u2026" if len(txt) > 1200 else "")


def extract_doi(*chunks):
    for chunk in chunks:
        if not chunk:
            continue
        m = DOI_RE.search(chunk)
        if m:
            return m.group(0).rstrip(".,;:)>]")
    return None


def entry_date(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return dt.datetime(*tm[:6], tzinfo=dt.timezone.utc).isoformat()
            except (TypeError, ValueError):
                pass
    return None


def fetch_feed(name, url, kind):
    """Recupere un flux et renvoie une liste d'items normalises."""
    try:
        resp = requests.get(url, timeout=25, headers={"User-Agent": UA})
        resp.raise_for_status()
    except Exception as exc:                      # noqa: BLE001
        print(f"  !! {name} : {type(exc).__name__} {exc}", file=sys.stderr)
        return []

    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        print(f"  !! {name} : flux vide ou illisible", file=sys.stderr)
        return []

    items = []
    for entry in parsed.entries:
        link = (entry.get("link") or entry.get("id") or "").strip()
        if not link:
            continue

        summary = entry.get("summary", "")
        if entry.get("content"):
            summary = summary + " " + entry["content"][0].get("value", "")

        items.append({
            "title": WS_RE.sub(" ", entry.get("title", "(sans titre)")).strip(),
            "link": link,
            "date": entry_date(entry),
            "source": name,
            "kind": kind,
            "abstract": clean_text(summary),
            "doi": extract_doi(link, summary, entry.get("dc_identifier", "")),
        })

    print(f"  {name} : {len(items)} items")
    return items


def altmetric(doi):
    """Interroge l'API Altmetric. None si absent ou en erreur."""
    try:
        r = requests.get(
            "https://api.altmetric.com/v1/doi/" + doi,
            timeout=20,
            headers={"User-Agent": UA},
        )
    except Exception:                             # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None

    outlets = []
    for story in (d.get("news_stories") or [])[:8]:
        label = story.get("outlet") or story.get("title")
        if label and label not in outlets:
            outlets.append(label)

    return {
        "score": round(float(d.get("score") or 0), 1),
        "outlets": outlets,
        "msm": int(d.get("cited_by_msm_count") or 0),
        "blogs": int(d.get("cited_by_blogs_count") or d.get("cited_by_blog_count") or 0),
        "tweets": int(d.get("cited_by_tweeters_count") or 0),
        "url": d.get("details_url") or "",
        "checked_at": now_iso(),
    }


def needs_altmetric(item, now):
    """Faut-il (re)interroger Altmetric pour cet article ?"""
    if not item.get("doi"):
        return False
    alt = item.get("alt")
    if alt is None:
        return True                               # jamais interroge

    checked = parse_iso(alt.get("checked_at"))
    if checked is None:
        return True
    if (now - checked).total_seconds() < ALT_RECHECK_HOURS * 3600:
        return False

    # On ne rafraichit que les articles encore recents : au-dela, la
    # couverture mediatique ne bouge quasiment plus.
    ref = parse_iso(item.get("date")) or parse_iso(item.get("first_seen"))
    if ref is None:
        return False
    return (now - ref).days <= ALT_FRESH_DAYS


def main():
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now_iso()

    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))

    # --- 1. Charger l'historique existant -----------------------------
    store = {}
    if OUT_FILE.exists():
        try:
            previous = json.loads(OUT_FILE.read_text(encoding="utf-8"))
            for item in previous.get("items", []):
                store[item["link"]] = item
        except (ValueError, KeyError):
            print("!! feed.json illisible, on repart de zero", file=sys.stderr)
    print(f"Historique existant : {len(store)} articles")

    # --- 2. Relever tous les flux -------------------------------------
    added = 0
    for kind in ("journal", "media"):
        print(f"\n[{kind}]")
        for src in sources.get(kind, []):
            for item in fetch_feed(src["name"], src["url"], kind):
                known = store.get(item["link"])
                if known:
                    # On rafraichit les metadonnees, on garde alt/first_seen
                    known.update({
                        k: item[k] for k in
                        ("title", "date", "source", "kind", "abstract")
                        if item.get(k)
                    })
                    if item.get("doi") and not known.get("doi"):
                        known["doi"] = item["doi"]
                else:
                    item["first_seen"] = stamp
                    item["alt"] = None
                    store[item["link"]] = item
                    added += 1
            time.sleep(0.3)
    print(f"\nNouveaux articles : {added}")

    # --- 3. Elaguer ----------------------------------------------------
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)

    def sort_key(item):
        ref = parse_iso(item.get("date")) or parse_iso(item.get("first_seen"))
        return ref or dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    items = [
        it for it in store.values()
        if (parse_iso(it.get("date")) or parse_iso(it.get("first_seen")) or now) >= cutoff
    ]
    items.sort(key=sort_key, reverse=True)
    items = items[:MAX_ITEMS]
    print(f"Apres elagage : {len(items)} articles")

    # --- 4. Altmetric ---------------------------------------------------
    calls = 0
    for item in items:
        if calls >= ALT_MAX_CALLS:
            break
        if not needs_altmetric(item, now):
            continue
        data = altmetric(item["doi"])
        calls += 1
        if data:
            item["alt"] = data
        elif item.get("alt") is None:
            # Marquer comme verifie pour ne pas reinterroger en boucle
            item["alt"] = {"score": 0, "outlets": [], "msm": 0, "blogs": 0,
                           "tweets": 0, "url": "", "checked_at": stamp}
        time.sleep(ALT_PAUSE)
    print(f"Appels Altmetric : {calls}")

    # --- 5. Ecrire -------------------------------------------------------
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": stamp,
        "counts": {
            "total": len(items),
            "journal": sum(1 for i in items if i["kind"] == "journal"),
            "media": sum(1 for i in items if i["kind"] == "media"),
            "new_this_run": added,
        },
        "items": items,
    }
    OUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(f"Ecrit : {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} Ko)")


if __name__ == "__main__":
    main()
