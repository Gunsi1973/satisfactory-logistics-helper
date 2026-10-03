#!/usr/bin/env python3
"""Satisfactory Logistics Helper - FRM -> Grist sync.

Periodically reads the live state of the game through the Ficsit Remote
Monitoring (FRM) mod and maintains these Grist tables:

  Stations     Name, Platforms, In_Game, Count_In_Game, Last_Seen
  Slots        Station, Slot, Mode, Content, Stock, Load_Status, In_Game,
               Game_ID and - only for Load with exactly one product - Load_Product
  Products     product names seen in the game are added
  Game_Vehicles  trains, trucks, drones - fully reconciled on every run
  Game_Ports     truck stations and drone ports - for information only
  Sync_Status  one row with status and timestamps

Never touched: Deliveries, and the manual fields Amount and Note.

Identity: station = name, slot = (station, number). The game's object IDs
change when a station is rebuilt or extended; they are stored for
information only. Slot number = rank of the platform by distance from the
station building (closest = 1).

When a station or platform disappears from the game:
  - if manual data is attached (amount, note, delivery, remaining slots),
    the row is only flagged as not in game,
  - otherwise it is deleted.

If FRM is unreachable or returns no stations, nothing but Sync_Status is
changed. Grist does not depend on this script.

Python standard library only.
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
GRIST_PUBLIC_URL = os.environ.get("GRIST_PUBLIC_URL", "http://localhost:8484").rstrip("/")
GRIST_DOC = os.environ.get("GRIST_DOC_ID", "")
GRIST_KEY = os.environ.get("GRIST_API_KEY", "")
DOC_NAME = os.environ.get("GRIST_DOC_NAME", "Satisfactory Logistics Helper")
TEMPLATE = os.environ.get("GRIST_TEMPLATE", "/app/template/satisfactory-logistics-helper.grist")
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


def grist_api(method, path, body=None):
    return http(method, "%s/api%s" % (GRIST_URL, path), body, {"Authorization": "Bearer " + GRIST_KEY})


def resolve_document():
    """Use GRIST_DOC_ID if set. Otherwise find the document named DOC_NAME,
    or import the bundled template once. Returns True when a document is known."""
    global GRIST_DOC
    if GRIST_DOC and GRIST_DOC != "xxx":
        return True
    workspaces = []
    for org in grist_api("GET", "/orgs"):
        workspaces += grist_api("GET", "/orgs/%d/workspaces" % org["id"])
    for ws in workspaces:
        for d in ws.get("docs") or []:
            if d.get("name") == DOC_NAME and not d.get("removedAt"):
                GRIST_DOC = d["id"]
                log("Using document '%s': %s/o/docs/%s" % (DOC_NAME, GRIST_PUBLIC_URL, GRIST_DOC))
                return True
    if not workspaces:
        log("No Grist workspace found for this API key")
        return False
    boundary = "----slh%d" % int(time.time() * 1000)
    with open(TEMPLATE, "rb") as f:
        payload = f.read()
    body = b"".join([
        ("--%s\r\nContent-Disposition: form-data; name=\"workspaceId\"\r\n\r\n%d\r\n" % (boundary, workspaces[0]["id"])).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"%s.grist\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, DOC_NAME)).encode(),
        payload, ("\r\n--%s--\r\n" % boundary).encode()])
    req = urllib.request.Request("%s/api/docs" % GRIST_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + GRIST_KEY, "Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        GRIST_DOC = json.loads(r.read())
    log("Created document '%s' from template: %s/o/docs/%s" % (DOC_NAME, GRIST_PUBLIC_URL, GRIST_DOC))
    return True


def records(table):
    return grist("GET", "/tables/%s/records" % table)["records"]


def add(table, rows):
    ids = []
    for i in range(0, len(rows), 200):
        res = grist("POST", "/tables/%s/records" % table, {"records": [{"fields": f} for f in rows[i:i + 200]]})
        ids += [r["id"] for r in res["records"]]
    return ids


def update(table, rows):
    """rows: list of (id, fields). Grist requires identical field names per PATCH, hence grouping."""
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
# Read FRM data
# ---------------------------------------------------------------------------

def inventory(items):
    inv = {}
    for it in items or []:
        inv[it["Name"]] = inv.get(it["Name"], 0) + it.get("Amount", 0)
    return inv


def transform(stations, trains):
    """Returns (stations, trains).

    stations: {name: {"count", "id", "platforms": [slot dicts]}}
    If several stations share a name, the one with the most platforms wins;
    the count is reported.
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
                "Mode": MODE.get(p.get("LoadingMode"), p.get("LoadingMode") or ""),
                "Content": ", ".join(sorted(inv)), "Stock": sum(inv.values()),
                "Load_Status": p.get("LoadingStatus") or "", "Game_ID": p["ID"],
                "_products": sorted(inv),
            })
        prev = result.get(s["Name"])
        entry = {"count": 1, "id": s["ID"], "platforms": slots}
        if prev:
            entry["count"] = prev["count"] + 1
            if len(prev["platforms"]) >= len(slots):
                entry.update(id=prev["id"], platforms=prev["platforms"])
        result[s["Name"]] = entry

    train_rows = []
    for t in trains:
        wagons = [v for v in t.get("Vehicles") or [] if "Locomotive" not in (v.get("ClassName") or "")]
        parts = []
        for i, v in enumerate(wagons, 1):
            inv = inventory(v.get("Inventory"))
            parts.append("%d: %s" % (i, ", ".join("%s %g" % (k, n) for k, n in sorted(inv.items())) or "empty"))
        train_rows.append({
            "Type": "Train", "Name": t.get("Name") or "", "Game_ID": t["ID"],
            "Route": u" \u2192 ".join(x.get("StationName", "?") for x in t.get("TimeTable") or []),
            "Status": t.get("Status") or "", "Current_Stop": t.get("TrainStation") or "",
            "Cargo": "\n".join(parts), "Fuel": "",
        })
    return result, train_rows


def fmt_items(items):
    inv = inventory(items)
    return ", ".join("%s %g" % (k, n) for k, n in sorted(inv.items()))


def transform_other(truck_stations, vehicles, drone_ports, drones):
    """Trucks, drones and their stations - listed for information only.

    Returns (vehicle_rows, port_rows) for Game_Vehicles and Game_Ports.
    FRM limits: truck stations report Idle/Transferring instead of
    Loading/Unloading, and trucks have no route information.
    """
    ports, vehicles_out = [], []
    for s in truck_stations:
        ports.append({
            "Type": "Truck station", "Name": s.get("Name") or "", "Game_ID": s["ID"], "Paired_With": "",
            "Status": " / ".join(x for x in (s.get("StationStatus"), s.get("LoadMode")) if x),
            "Inventory": fmt_items(s.get("Inventory")), "Received": "",
            "Fuel": fmt_items(s.get("FuelInventory")), "Est_Rate": None,
        })
    for s in drone_ports:
        ports.append({
            "Type": "Drone port", "Name": s.get("Name") or "", "Game_ID": s["ID"],
            "Paired_With": s.get("PairedStation") or "", "Status": s.get("DroneStatus") or "",
            "Inventory": fmt_items(s.get("InputInventory")), "Received": fmt_items(s.get("OutputInventory")),
            "Fuel": fmt_items(s.get("FuelInventory")), "Est_Rate": s.get("EstTotalTransRate"),
        })
    for v in vehicles:
        vx, vy = v["location"]["x"], v["location"]["y"]
        near = [s["Name"] for s in truck_stations
                if math.hypot(s["location"]["x"] - vx, s["location"]["y"] - vy) < 3000]
        status = "Autopilot" if v.get("Autopilot") else "Manual"
        if v.get("AutoPilotStatus") not in (None, "", "None"):
            status += " (%s)" % v["AutoPilotStatus"]
        if v.get("HasFuel") is False:
            status += ", no fuel"
        vehicles_out.append({
            "Type": v.get("Name") or v.get("ClassName") or "Vehicle", "Name": v.get("Name") or "",
            "Game_ID": v["ID"], "Route": "", "Status": status,
            "Current_Stop": ("at " + near[0]) if near else "",
            "Cargo": fmt_items(v.get("Inventory")) or "empty", "Fuel": fmt_items(v.get("FuelInventory")),
        })
    for d in drones:
        vehicles_out.append({
            "Type": "Drone", "Name": "Drone (%s)" % (d.get("HomeStation") or "?"), "Game_ID": d["ID"],
            "Route": u"%s \u21c4 %s" % (d.get("HomeStation") or "?", d.get("PairedStation") or "not paired"),
            "Status": d.get("CurrentFlyingMode") or "",
            "Current_Stop": (u"\u2192 " + d["CurrentDestination"]) if d.get("CurrentDestination") else "",
            "Cargo": "", "Fuel": "",
        })
    return vehicles_out, ports


# ---------------------------------------------------------------------------
# Reconcile Grist
# ---------------------------------------------------------------------------

def sync_products(game):
    have = {r["fields"]["Name"]: r["id"] for r in records("Products")}
    needed = sorted({p for st in game.values() for s in st["platforms"] for p in s["_products"]} - set(have))
    if needed:
        for name, rid in zip(needed, add("Products", [{"Name": n} for n in needed])):
            have[name] = rid
        log("New products: %s" % ", ".join(needed))
    return have


def sync_stations(game, now):
    existing = {r["fields"]["Name"]: r for r in records("Stations")}
    new = [n for n in game if n not in existing]
    ids = {n: r["id"] for n, r in existing.items()}
    if new:
        for n, rid in zip(new, add("Stations", [{"Name": n} for n in new])):
            ids[n] = rid
        log("New stations: %s" % ", ".join(new))
    update("Stations", [(ids[n], {"Platforms": len(st["platforms"]), "In_Game": True,
                                  "Count_In_Game": st["count"], "Last_Seen": now})
                        for n, st in game.items()])
    return ids, existing


def sync_slots(game, station_ids, products):
    existing = {(r["fields"]["Station"], r["fields"]["Slot"]): r for r in records("Slots")}
    seen, to_add, to_update = set(), [], []
    for name, st in game.items():
        sid = station_ids[name]
        for s in st["platforms"]:
            key = (sid, s["Slot"])
            seen.add(key)
            fields = {k: v for k, v in s.items() if not k.startswith("_")}
            fields["In_Game"] = True
            if s["Mode"] == "Load" and len(s["_products"]) == 1:
                fields["Load_Product"] = products[s["_products"][0]]
            if key in existing:
                old = existing[key]["fields"]
                if any(old.get(k) != v for k, v in fields.items()):
                    to_update.append((existing[key]["id"], fields))
            else:
                fields.update(Station=sid, Slot=s["Slot"])
                to_add.append(fields)
    add("Slots", to_add)
    update("Slots", to_update)

    # Slots that no longer exist in the game
    used_as_source = {r["fields"]["Source"] for r in records("Deliveries")}
    drop, mark = [], []
    for key, r in existing.items():
        if key in seen:
            continue
        f = r["fields"]
        if f.get("Amount") or f.get("Note") or r["id"] in used_as_source:
            if f.get("In_Game") is not False:
                mark.append((r["id"], {"In_Game": False}))
        else:
            drop.append(r["id"])
    update("Slots", mark)
    delete("Slots", drop)
    return len(to_add), len(drop) + len(mark)


def retire_stations(game, existing):
    gone = [r for n, r in existing.items() if n not in game]
    if not gone:
        return 0
    slot_owner = {r["fields"]["Station"] for r in records("Slots")}
    targets = {r["fields"]["To"] for r in records("Deliveries")}
    drop, mark = [], []
    for r in gone:
        if r["fields"].get("Note") or r["id"] in slot_owner or r["id"] in targets:
            if r["fields"].get("In_Game") is not False:
                mark.append((r["id"], {"In_Game": False}))
        else:
            drop.append(r["id"])
    update("Stations", mark)
    delete("Stations", drop)
    return len(gone)


def sync_by_game_id(table, rows):
    """Full reconcile of a pure game table (no manual data) keyed by Game_ID."""
    existing = {r["fields"]["Game_ID"]: r["id"] for r in records(table)}
    want = {r["Game_ID"] for r in rows}
    add(table, [r for r in rows if r["Game_ID"] not in existing])
    update(table, [(existing[r["Game_ID"]], r) for r in rows if r["Game_ID"] in existing])
    delete(table, [rid for gid, rid in existing.items() if gid not in want])


def frm_optional(endpoint):
    """Endpoints for trucks and drones: missing or failing ones just yield an empty list."""
    try:
        return frm(endpoint) or []
    except (urllib.error.URLError, OSError, ValueError):
        return []


def set_status(status, success):
    now = time.time()
    fields = {"Status": status, "Last_Attempt": now}
    if success:
        fields["Last_Success"] = now
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
        msg = "FRM not reachable (%s) - keeping data from the last successful sync" % getattr(e, "reason", e)
        log(msg)
        set_status(msg, False)
        return
    if not stations:
        msg = "FRM returned no stations (game still loading?) - keeping data"
        log(msg)
        set_status(msg, False)
        return
    now = time.time()
    game, train_rows = transform(stations, trains or [])
    other_vehicles, port_rows = transform_other(frm_optional("getTruckStation"), frm_optional("getVehicles"),
                                                frm_optional("getDroneStation"), frm_optional("getDrone"))
    products = sync_products(game)
    station_ids, existing_stations = sync_stations(game, now)
    added, retired = sync_slots(game, station_ids, products)
    gone = retire_stations(game, existing_stations)
    sync_by_game_id("Game_Vehicles", train_rows + other_vehicles)
    sync_by_game_id("Game_Ports", port_rows)
    n_slots = sum(len(s["platforms"]) for s in game.values())
    msg = "OK: %d stations, %d platforms, %d trains" % (len(game), n_slots, len(train_rows))
    if other_vehicles or port_rows:
        msg += ", %d other vehicles, %d truck stations/drone ports" % (len(other_vehicles), len(port_rows))
    extra = []
    if added:
        extra.append("%d new slots" % added)
    if retired:
        extra.append("%d slots no longer in game" % retired)
    if gone:
        extra.append("%d stations no longer in game" % gone)
    dup = [n for n, s in game.items() if s["count"] > 1]
    if dup:
        extra.append("duplicate station names: %s" % ", ".join(dup))
    if extra:
        msg += " (" + "; ".join(extra) + ")"
    set_status(msg, True)
    log(msg)


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not GRIST_KEY or GRIST_KEY == "xxx":
        log("GRIST_API_KEY missing in .env - sync paused. In Grist: avatar -> Profile Settings -> API -> Create, "
            "put the key into .env, then run: docker compose up -d")
        while True:
            time.sleep(3600)
    log("Start: FRM %s -> Grist %s, interval %ds" % (FRM_URL, GRIST_URL, INTERVAL))
    once = "--once" in sys.argv
    while True:
        wait = INTERVAL
        try:
            if resolve_document():
                sync_once()
            else:
                wait = 15
        except urllib.error.HTTPError as e:
            log("Grist error %s: %s" % (e.code, e.read()[:300]))
        except Exception as e:  # never crash; retry on the next interval
            log("Error: %r" % (e,))
            if not GRIST_DOC or GRIST_DOC == "xxx":
                wait = 15  # Grist probably still starting
        if once:
            return
        time.sleep(wait)


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        g, t = transform(frm("getTrainStation"), frm("getTrains"))
        ov, po = transform_other(frm_optional("getTruckStation"), frm_optional("getVehicles"),
                                 frm_optional("getDroneStation"), frm_optional("getDrone"))
        print(json.dumps({"stations": g, "vehicles": t + ov, "ports": po}, indent=1, ensure_ascii=False))
    else:
        main()
