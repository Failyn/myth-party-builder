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
           "main": bool(entry.get("main")), "order": entry["order"], "cid": cid}
    if il < old_il * SMALL_DROP:
        # count only runs where the site keeps giving the same lower number; glitches vary
        same = il > 0 and il == int((prev or {}).get("ilSeen") or -1)
        held = (int((prev or {}).get("ilHeld") or 0) + 1) if same else 1
        if held < ACCEPT_AFTER:
            doc["il"], doc["ilHeld"], doc["ilSeen"] = old_il, held, il
    return doc, samples


def read_previous(url, key):
    req = urllib.request.Request(url + "/rest/v1/docs?coll=eq.chars&select=id,data",
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
    restore_path = os.path.join(HERE, "..", "restore_il.json")
    if os.path.exists(restore_path):  # one-off: known-good item levels to recover from bad data
        for cid_, value in json.load(open(restore_path)).items():
            old = previous.setdefault(cid_, {})
            if value > int(old.get("il") or 0):
                old["il"], old["ilHeld"] = value, 0
    rows, failed = [], []
    for e in roster:
        try:
            doc, samples = fetch(e, previous.get(e["id"]))
            doc["readAtIso"] = now
            rows.append({"coll": "chars", "id": e["id"], "data": doc, "updated_at": now})
            note = (" (held; site said %s, run %s of %s)" % (doc["ilSeen"], doc["ilHeld"], ACCEPT_AFTER)) if doc.get("ilHeld") else ""
            print("ok   %-10s Lv%-2s %-12s IL %-5s CP %-7s asked %sx%s" % (e["id"], doc["level"], doc["cls"], doc["il"], doc["cp"], samples, note))
        except Exception as ex:  # keep the previous numbers for this character
            failed.append(e["id"])
            print("FAIL %-10s %s" % (e["id"], ex))
        time.sleep(0.2)
    if dry:
        print("dry run: %d fetched, %d failed, nothing written" % (len(rows), len(failed)))
        return 0 if rows else 1
    head = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if rows:
        req = urllib.request.Request(url + "/rest/v1/docs", data=json.dumps(rows).encode("utf-8"), method="POST",
                                     headers=dict(head, Prefer="resolution=merge-duplicates,return=minimal"))
        urllib.request.urlopen(req, timeout=30).read()
    # remove characters that are no longer in roster.json
    keep = ",".join('"%s"' % e["id"] for e in roster)
    req = urllib.request.Request(url + "/rest/v1/docs?coll=eq.chars&id=not.in.(%s)" % urllib.parse.quote(keep), method="DELETE", headers=head)
    urllib.request.urlopen(req, timeout=30).read()
    print("wrote %d characters, %d failed" % (len(rows), len(failed)))
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
