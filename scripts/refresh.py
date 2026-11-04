#!/usr/bin/env python3
"""Refresh item level and combat power for every character in roster.json.

Reads the official AION 2 character pages and writes the results to the
shared database. Needs SUPABASE_URL and SUPABASE_SERVICE_KEY in the
environment. Run with --dry-run to print the results without writing.
"""
import datetime, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

SERVER = 1301
REGION = "eu"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HERE = os.path.dirname(os.path.abspath(__file__))


def get_json(url, params, headers=None, tries=2):
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(full, headers=dict({"User-Agent": UA, "Accept": "application/json"}, **(headers or {})))
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == tries - 1:
                raise RuntimeError("%s -> %s" % (url, e))
            time.sleep(2 + attempt * 3)


def find_id(name):
    data = get_json("https://api-search.plaync.com/aion2global/search/v2/character",
                    {"keyword": name, "serverId": SERVER, "page": 1, "size": 40, "region": REGION, "localeInfo": "en-US"})
    for c in data.get("list", []):
        if re.sub(r"<[^>]+>", "", c.get("name", "")).lower() == name.lower():
            return urllib.parse.unquote(c["characterId"])
    return None


SMALL_DROP = 0.97   # a real item level change is small; the site's glitches are large
MAX_SAMPLES = 3     # how many times to ask when the number looks wrong
ACCEPT_AFTER = 72   # hourly runs showing the same lower number before it is believed


def read_once(cid):
    info = get_json("https://aion2.plaync.com/api/character/info",
                    {"region": REGION, "lang": "en-US", "characterId": cid, "serverId": SERVER})
    il = [s["value"] for s in info["stat"]["statList"] if s.get("type") == "ItemLevel"]
    return info["profile"], (il[0] if il else 0)


def fetch(entry, prev):
    """The official page often reports an item level of 0 or a partial sum.
    Ask several times, keep the highest, and hold the previous value if the
    best answer is still far below it."""
    cid = entry.get("cid") or (prev or {}).get("cid") or find_id(entry["name"])
    if not cid:
        raise RuntimeError("no character named %s on the server" % entry["name"])
    old_il = int((prev or {}).get("il") or 0)
    profile, il = read_once(cid)
    samples = 1
    while il < old_il * SMALL_DROP and samples < MAX_SAMPLES:
        time.sleep(0.6)
        profile, again = read_once(cid)
        il = max(il, again)
        samples += 1
    doc = {"name": profile["characterName"], "player": entry["player"], "cls": profile["className"],
           "level": profile["characterLevel"], "il": il, "cp": profile["combatPower"] or (prev or {}).get("cp", 0),
           "main": bool(entry.get("main")), "order": entry["order"], "cid": cid,
           "img": profile.get("profileImage") or (prev or {}).get("img", "")}
    if il < old_il * SMALL_DROP:
        # count only runs where the site keeps giving the same lower number; glitches vary
        same = il > 0 and il == int((prev or {}).get("ilSeen") or -1)
        held = (int((prev or {}).get("ilHeld") or 0) + 1) if same else 1
        if held < ACCEPT_AFTER:
            doc["il"], doc["ilHeld"], doc["ilSeen"] = old_il, held, il
    return doc, samples


def track_high(doc, prev, series, now_min, field):
    """Keep the highest value seen for field ("cp" or "il"). A new high only
    counts once the next run backs it up (the site once showed one character at
    double CP for an hour). Each new high is added to series. True if it grew."""
    prev = prev or {}
    val, last = int(doc[field] or 0), int(prev.get(field + "Last") or 0)
    best = int(prev.get(field + "Max") or 0)
    confirmed = min(val, last) if last else val
    doc[field + "Last"] = val
    if confirmed > best:
        doc[field + "Max"] = confirmed
        series.append([now_min, confirmed])
        return True
    doc[field + "Max"] = best or val
    return False


WORN = {"MainHand", "SubHand", "Helmet", "Shoulder", "Torso", "Pants", "Gloves", "Boots", "Cape", "Belt",
        "Necklace", "Amulet", "Earring1", "Earring2", "Ring1", "Ring2", "Bracelet1", "Bracelet2", "Rune1", "Rune2"}
DETAIL_KEEP = ("level", "categoryName", "equipLevel", "maxEnchantLevel", "maxExceedEnchantLevel", "magicStoneSlotCount",
               "godStoneSlotCount", "mainStats", "subStats", "magicStoneStat", "godStoneStat", "soulBindRate", "sources")
FULL_REFRESH_H = 20   # re-read every item about once a day (manastones and rerolled stats don't change the item id)


def item_key(it):
    return "%s:%s:%s" % (it.get("id"), it.get("enchantLevel"), it.get("exceedLevel"))


def read_gear(cid, prev_gear):
    """Worn equipment for one character. Item details (stats, manastones, godstone) are fetched only for
    items that changed since last time, or for everything once a day. Returns (doc, details fetched)."""
    from concurrent.futures import ThreadPoolExecutor
    base = {"region": REGION, "lang": "en-US", "characterId": cid, "serverId": SERVER}
    lst = get_json("https://aion2.plaync.com/api/character/equipment", base)["equipment"]["equipmentList"]
    worn = [it for it in lst if it.get("slotPosName") in WORN]
    prev_items = {x["slot"]: x for x in (prev_gear or {}).get("items", [])}
    now = time.time()
    full = now - float((prev_gear or {}).get("detAt") or 0) > FULL_REFRESH_H * 3600
    def detail(it):
        old = prev_items.get(it["slotPosName"])
        if not full and old and old.get("key") == item_key(it) and old.get("d"):
            return old["d"], False
        try:
            d = get_json("https://aion2.plaync.com/api/character/equipment/item",
                         {"id": it["id"], "enchantLevel": it.get("enchantLevel", 0), "characterId": cid, "serverId": SERVER,
                          "slotPos": it["slotPos"], "lang": "en-US", "region": REGION})
            return {k: d[k] for k in DETAIL_KEEP if k in d}, True
        except Exception:
            return (old or {}).get("d"), False
    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(detail, worn))
    items = [{"slot": it["slotPosName"], "id": it["id"], "name": it["name"], "grade": it.get("grade", ""), "icon": it.get("icon", ""),
              "enchant": it.get("enchantLevel", 0), "exceed": it.get("exceedLevel", 0), "key": item_key(it), "d": d}
             for it, (d, _) in zip(worn, got)]
    fetched = sum(1 for _, f in got if f)
    det_at = now if full else (prev_gear or {}).get("detAt") or now
    return {"items": items, "detAt": det_at}, fetched


def read_previous(url, key, coll="chars"):
    req = urllib.request.Request(url + "/rest/v1/docs?coll=eq.%s&select=id,data" % coll,
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return {row["id"]: row["data"] for row in json.loads(r.read().decode("utf-8"))}


def main():
    dry = "--dry-run" in sys.argv
    roster = json.load(open(os.path.join(HERE, "..", "roster.json"), encoding="utf-8"))
    roster = [e for e in roster if not e.get("hidden")]  # "hidden": true parks a character without losing its entry
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    previous = read_previous(url, key) if url and key else {}
    stored = read_previous(url, key, "cphist") if url and key else {}
    history = {k: {"pts": v.get("pts", []), "il": v.get("il")} for k, v in stored.items()}
    seed_path = os.path.join(HERE, "..", "cp_seed.json")
    seed = json.load(open(seed_path)) if os.path.exists(seed_path) else {}
    grown = set()  # histories that need writing
    for cid_, sv in seed.items():  # one-off: history rebuilt from old run logs, used only where none is stored yet
        h, old = history.setdefault(cid_, {"pts": None, "il": None}), previous.setdefault(cid_, {})
        for field, key_ in (("cp", "pts"), ("il", "il")):
            if h.get(key_) is None and key_ in sv:
                h[key_] = sv[key_]; grown.add(cid_)
                old.setdefault(field + "Max", sv[field + "Max"]); old.setdefault(field + "Last", sv[field + "Last"])
    for h in history.values():
        h["pts"], h["il"] = h.get("pts") or [], h.get("il") or []
    now_min = int(time.time() // 60)
    restore_path = os.path.join(HERE, "..", "restore_il.json")
    if os.path.exists(restore_path):  # one-off: known-good item levels to recover from bad data
        for cid_, value in json.load(open(restore_path)).items():
            old = previous.setdefault(cid_, {})
            if value > int(old.get("il") or 0):
                old["il"], old["ilHeld"] = value, 0
    stored_gear = read_previous(url, key, "gear") if url and key else {}
    rows, failed, gear_rows = [], [], []
    for e in roster:
        try:
            doc, samples = fetch(e, previous.get(e["id"]))
            doc["readAtIso"] = now
            h = history.setdefault(e["id"], {"pts": [], "il": []})
            if track_high(doc, previous.get(e["id"]), h["pts"], now_min, "cp") | track_high(doc, previous.get(e["id"]), h["il"], now_min, "il"):
                grown.add(e["id"])
            rows.append({"coll": "chars", "id": e["id"], "data": doc, "updated_at": now})
            try:
                g, nd = read_gear(doc["cid"], stored_gear.get(e["id"]))
                g.update(name=doc["name"], at=now)
                if json.dumps(g.get("items"), sort_keys=True) != json.dumps((stored_gear.get(e["id"]) or {}).get("items"), sort_keys=True) or g["detAt"] != (stored_gear.get(e["id"]) or {}).get("detAt"):
                    gear_rows.append({"coll": "gear", "id": e["id"], "data": g, "updated_at": now})
                gnote = " gear %d items, %d details" % (len(g["items"]), nd)
            except Exception as gex:  # gear never blocks the stats
                gnote = " gear FAILED (%s)" % gex
            note = (" (held; site said %s, run %s of %s)" % (doc["ilSeen"], doc["ilHeld"], ACCEPT_AFTER)) if doc.get("ilHeld") else ""
            print("ok   %-10s Lv%-2s %-12s IL %-5s CP %-7s max %-7s IL max %-5s asked %sx%s%s" % (e["id"], doc["level"], doc["cls"], doc["il"], doc["cp"], doc["cpMax"], doc["ilMax"], samples, note, gnote))
        except Exception as ex:  # keep the previous numbers for this character
            failed.append(e["id"])
            print("FAIL %-10s %s" % (e["id"], ex))
        time.sleep(0.2)
    if dry:
        if gear_rows: json.dump(gear_rows, open(os.environ.get("GEAR_OUT", "/dev/null"), "w"))
        print("dry run: %d fetched, %d failed, nothing written" % (len(rows), len(failed)))
        return 0 if rows else 1
    head = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    names = {k: v.get("name") for k, v in previous.items()}
    names.update({r["id"]: r["data"]["name"] for r in rows})
    rows += [{"coll": "cphist", "id": k, "data": {"name": names.get(k) or k, "pts": history[k]["pts"], "il": history[k]["il"]}, "updated_at": now}
             for k in sorted(grown) if k in history]
    n_gear = len(gear_rows)
    rows += gear_rows
    if rows:
        req = urllib.request.Request(url + "/rest/v1/docs", data=json.dumps(rows).encode("utf-8"), method="POST",
                                     headers=dict(head, Prefer="resolution=merge-duplicates,return=minimal"))
        urllib.request.urlopen(req, timeout=30).read()
    # remove characters that are no longer in roster.json
    keep = ",".join('"%s"' % e["id"] for e in roster)
    req = urllib.request.Request(url + "/rest/v1/docs?coll=eq.chars&id=not.in.(%s)" % urllib.parse.quote(keep), method="DELETE", headers=head)
    urllib.request.urlopen(req, timeout=30).read()
    print("wrote %d characters, %d CP histories and %d gear sets, %d failed" % (len(rows) - len(grown) - n_gear, len(grown), n_gear, len(failed)))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
