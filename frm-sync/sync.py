#!/usr/bin/env python3
"""FRM -> Grist Sync fuer das Satisfactory-Bahnhof-Tracking.

Liest periodisch den Ist-Zustand aus dem Spiel ueber die Ficsit Remote
Monitoring (FRM) Schnittstelle und pflegt damit in Grist:

  Bahnhoefe   Name, Plattformen, Im_Spiel, Anzahl_im_Spiel, Zuletzt_gesehen
  Slots       Bahnhof, Slot, Modus, Inhalt, Bestand, Ladestatus, Im_Spiel,
              Spiel_ID und - nur bei Load mit genau einem Produkt - Ladegut
  Produkte    neue Produktnamen aus dem Spiel werden ergaenzt
  Spiel_Zuege wird bei jedem Lauf vollstaendig abgeglichen
  Sync_Status eine Zeile mit Status und Zeitstempeln

Nie angefasst: Lieferungen, sowie die manuellen Felder Menge und Notiz.

Identitaet: Bahnhof = Name, Slot = (Bahnhof, Nummer). Die Spiel-IDs aendern
sich beim Neubau oder Verlaengern eines Bahnhofs und werden nur zur
Information gespeichert. Slot-Nummer = Rang der Plattform nach Abstand zum
Bahnhofsgebaeude (naechste = 1).

Verschwindet ein Bahnhof oder eine Plattform aus dem Spiel:
  - haengen manuelle Daten dran (Menge, Notiz, Lieferung, verbliebene
    Slots), wird die Zeile nur als "nicht mehr im Spiel" markiert,
  - sonst wird sie geloescht.

Ist FRM nicht erreichbar oder liefert es keine Bahnhoefe, wird nichts
geaendert ausser Sync_Status. Grist haengt nicht von diesem Skript ab.

Nur Python-Standardbibliothek.
"""
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

FRM_URL = os.environ.get("FRM_URL", "http://127.0.0.1:8080").rstrip("/")
GRIST_URL = os.environ.get("GRIST_URL", "http://127.0.0.1:8484").rstrip("/")
GRIST_DOC = os.environ.get("GRIST_DOC_ID", "")
GRIST_KEY = os.environ.get("GRIST_API_KEY", "")
INTERVAL = int(os.environ.get("SYNC_INTERVAL", "120"))
TIMEOUT = 15

MODE = {"Loading": "Load", "Unloading": "Unload"}


def log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def http(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def frm(endpoint):
    return http("GET", "%s/%s" % (FRM_URL, endpoint))


def grist(method, path, body=None):
    return http(method, "%s/api/docs/%s%s" % (GRIST_URL, GRIST_DOC, path), body,
                {"Authorization": "Bearer " + GRIST_KEY})


def records(table):
    return grist("GET", "/tables/%s/records" % table)["records"]


def add(table, rows):
    ids = []
    for i in range(0, len(rows), 200):
        res = grist("POST", "/tables/%s/records" % table, {"records": [{"fields": f} for f in rows[i:i + 200]]})
        ids += [r["id"] for r in res["records"]]
    return ids


def update(table, rows):
    """rows: Liste von (id, fields). Grist verlangt pro PATCH identische Feldnamen, daher gruppiert."""
    groups = {}
    for rid, f in rows:
        groups.setdefault(tuple(sorted(f)), []).append((rid, f))
    for batch in groups.values():
        for i in range(0, len(batch), 200):
            grist("PATCH", "/tables/%s/records" % table,
                  {"records": [{"id": rid, "fields": f} for rid, f in batch[i:i + 200]]})


def delete(table, ids):
    if ids:
        grist("POST", "/tables/%s/data/delete" % table, list(ids))


# ---------------------------------------------------------------------------
# FRM-Daten aufbereiten
# ---------------------------------------------------------------------------

def inventory(items):
    inv = {}
    for it in items or []:
        inv[it["Name"]] = inv.get(it["Name"], 0) + it.get("Amount", 0)
    return inv


def transform(stations, trains):
    """Liefert (bahnhoefe, zuege).

    bahnhoefe: {Name: {"anzahl", "id", "plattformen": [slot-dicts]}}
    Bei mehreren gleichnamigen Bahnhoefen gewinnt der mit den meisten
    Plattformen; die Anzahl wird gemeldet.
    """
    result = {}
    for s in stations:
        sx, sy = s["location"]["x"], s["location"]["y"]
        plats = sorted(s.get("CargoInventory") or [],
                       key=lambda p: math.hypot(p["location"]["x"] - sx, p["location"]["y"] - sy))
        slots = []
        for i, p in enumerate(plats, 1):
            inv = inventory(p.get("Inventory"))
            slots.append({
                "Slot": i,
                "Modus": MODE.get(p.get("LoadingMode"), p.get("LoadingMode") or ""),
                "Inhalt": ", ".join(sorted(inv)), "Bestand": sum(inv.values()),
                "Ladestatus": p.get("LoadingStatus") or "", "Spiel_ID": p["ID"],
                "_produkte": sorted(inv),
            })
        prev = result.get(s["Name"])
        entry = {"anzahl": 1, "id": s["ID"], "plattformen": slots}
        if prev:
            entry["anzahl"] = prev["anzahl"] + 1
            if len(prev["plattformen"]) >= len(slots):
                entry.update(id=prev["id"], plattformen=prev["plattformen"])
        result[s["Name"]] = entry

    train_rows = []
    for t in trains:
        wagons = [v for v in t.get("Vehicles") or [] if "Locomotive" not in (v.get("ClassName") or "")]
        parts = []
        for i, v in enumerate(wagons, 1):
            inv = inventory(v.get("Inventory"))
            parts.append("%d: %s" % (i, ", ".join("%s %g" % (k, n) for k, n in sorted(inv.items())) or "leer"))
        train_rows.append({
            "Name": t.get("Name") or "", "Spiel_ID": t["ID"],
            "Fahrplan": u" \u2192 ".join(x.get("StationName", "?") for x in t.get("TimeTable") or []),
            "Status": t.get("Status") or "", "Aktueller_Halt": t.get("TrainStation") or "",
            "Wagen": "\n".join(parts),
        })
    return result, train_rows


# ---------------------------------------------------------------------------
# Grist abgleichen
# ---------------------------------------------------------------------------

def sync_products(game):
    have = {r["fields"]["Name"]: r["id"] for r in records("Produkte")}
    needed = sorted({p for st in game.values() for s in st["plattformen"] for p in s["_produkte"]} - set(have))
    if needed:
        for name, rid in zip(needed, add("Produkte", [{"Name": n} for n in needed])):
            have[name] = rid
        log("Neue Produkte: %s" % ", ".join(needed))
    return have


def sync_stations(game, now):
    existing = {r["fields"]["Name"]: r for r in records("Bahnhoefe")}
    new = [n for n in game if n not in existing]
    ids = {n: r["id"] for n, r in existing.items()}
    if new:
        for n, rid in zip(new, add("Bahnhoefe", [{"Name": n} for n in new])):
            ids[n] = rid
        log("Neue Bahnhoefe: %s" % ", ".join(new))
    update("Bahnhoefe", [(ids[n], {"Plattformen": len(st["plattformen"]), "Im_Spiel": True,
                                   "Anzahl_im_Spiel": st["anzahl"], "Zuletzt_gesehen": now})
                         for n, st in game.items()])
    return ids, existing


def sync_slots(game, station_ids, products):
    existing = {(r["fields"]["Bahnhof"], r["fields"]["Slot"]): r for r in records("Slots")}
    seen, to_add, to_update = set(), [], []
    for name, st in game.items():
        bid = station_ids[name]
        for s in st["plattformen"]:
            key = (bid, s["Slot"])
            seen.add(key)
            fields = {k: v for k, v in s.items() if not k.startswith("_")}
            fields["Im_Spiel"] = True
            if s["Modus"] == "Load" and len(s["_produkte"]) == 1:
                fields["Ladegut"] = products[s["_produkte"][0]]
            if key in existing:
                old = existing[key]["fields"]
                if any(old.get(k) != v for k, v in fields.items()):
                    to_update.append((existing[key]["id"], fields))
            else:
                fields.update(Bahnhof=bid, Slot=s["Slot"])
                to_add.append(fields)
    add("Slots", to_add)
    update("Slots", to_update)

    # Slots, die im Spiel nicht mehr existieren
    used_as_source = {r["fields"]["Quelle"] for r in records("Lieferungen")}
    drop, mark = [], []
    for key, r in existing.items():
        if key in seen:
            continue
        f = r["fields"]
        if f.get("Menge") or f.get("Notiz") or r["id"] in used_as_source:
            if f.get("Im_Spiel") is not False:
                mark.append((r["id"], {"Im_Spiel": False}))
        else:
            drop.append(r["id"])
    update("Slots", mark)
    delete("Slots", drop)
    return len(to_add), len(drop) + len(mark)


def retire_stations(game, existing):
    gone = [r for n, r in existing.items() if n not in game]
    if not gone:
        return 0
    slot_owner = {r["fields"]["Bahnhof"] for r in records("Slots")}
    targets = {r["fields"]["Nach"] for r in records("Lieferungen")}
    drop, mark = [], []
    for r in gone:
        if r["fields"].get("Notiz") or r["id"] in slot_owner or r["id"] in targets:
            if r["fields"].get("Im_Spiel") is not False:
                mark.append((r["id"], {"Im_Spiel": False}))
        else:
            drop.append(r["id"])
    update("Bahnhoefe", mark)
    delete("Bahnhoefe", drop)
    return len(gone)


def sync_trains(rows):
    existing = {r["fields"]["Spiel_ID"]: r["id"] for r in records("Spiel_Zuege")}
    want = {r["Spiel_ID"] for r in rows}
    add("Spiel_Zuege", [r for r in rows if r["Spiel_ID"] not in existing])
    update("Spiel_Zuege", [(existing[r["Spiel_ID"]], r) for r in rows if r["Spiel_ID"] in existing])
    delete("Spiel_Zuege", [rid for sid, rid in existing.items() if sid not in want])


def set_status(status, success):
    now = time.time()
    fields = {"Status": status, "Letzter_Versuch": now}
    if success:
        fields["Letzter_Erfolg"] = now
    existing = records("Sync_Status")
    if existing:
        update("Sync_Status", [(existing[0]["id"], fields)])
    else:
        add("Sync_Status", [fields])


def sync_once():
    try:
        stations = frm("getTrainStation")
        trains = frm("getTrains")
    except (urllib.error.URLError, OSError, ValueError) as e:
        msg = "FRM nicht erreichbar (%s) - Daten vom letzten Erfolg bleiben stehen" % getattr(e, "reason", e)
        log(msg)
        set_status(msg, False)
        return
    if not stations:
        msg = "FRM liefert keine Bahnhoefe (Spiel noch am Laden?) - Daten bleiben stehen"
        log(msg)
        set_status(msg, False)
        return
    now = time.time()
    game, train_rows = transform(stations, trains or [])
    products = sync_products(game)
    station_ids, existing_stations = sync_stations(game, now)
    added, retired = sync_slots(game, station_ids, products)
    gone = retire_stations(game, existing_stations)
    sync_trains(train_rows)
    n_slots = sum(len(s["plattformen"]) for s in game.values())
    msg = "OK: %d Bahnhoefe, %d Plattformen, %d Zuege" % (len(game), n_slots, len(train_rows))
    extra = []
    if added:
        extra.append("%d Slots neu" % added)
    if retired:
        extra.append("%d Slots nicht mehr im Spiel" % retired)
    if gone:
        extra.append("%d Bahnhoefe nicht mehr im Spiel" % gone)
    dup = [n for n, s in game.items() if s["anzahl"] > 1]
    if dup:
        extra.append("Name mehrfach vergeben: %s" % ", ".join(dup))
    if extra:
        msg += " (" + "; ".join(extra) + ")"
    set_status(msg, True)
    log(msg)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not GRIST_DOC or not GRIST_KEY or GRIST_KEY == "xxx":
        log("GRIST_API_KEY fehlt in der .env - Sync pausiert. Key eintragen, dann: "
            "docker compose up -d satisfactory-frm-sync")
        while True:
            time.sleep(3600)
    log("Start: FRM %s -> Grist %s (Dokument %s), Intervall %ds" % (FRM_URL, GRIST_URL, GRIST_DOC, INTERVAL))
    while True:
        try:
            sync_once()
        except urllib.error.HTTPError as e:
            log("Grist-Fehler %s: %s" % (e.code, e.read()[:300]))
        except Exception as e:  # nie abstuerzen, naechster Versuch im naechsten Intervall
            log("Fehler: %r" % (e,))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        g, t = transform(frm("getTrainStation"), frm("getTrains"))
        print(json.dumps({"bahnhoefe": g, "zuege": t}, indent=1, ensure_ascii=False))
    else:
        main()
