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


def get_json(url, params, headers=None, tries=3):
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(full, headers=dict({"User-Agent": UA, "Accept": "application/json"}, **(headers or {})))
            with urllib.request.urlopen(req, timeout=30) as r:
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


def fetch(entry):
    cid = entry.get("cid") or find_id(entry["name"])
    if not cid:
        raise RuntimeError("no character named %s on the server" % entry["name"])
    info = get_json("https://aion2.plaync.com/api/character/info",
                    {"region": REGION, "lang": "en-US", "characterId": cid, "serverId": SERVER})
    p = info["profile"]
    il = [s["value"] for s in info["stat"]["statList"] if s.get("type") == "ItemLevel"]
    return {"name": p["characterName"], "player": entry["player"], "cls": p["className"], "level": p["characterLevel"],
            "il": il[0] if il else 0, "cp": p["combatPower"], "main": bool(entry.get("main")), "order": entry["order"], "cid": cid}


def main():
    dry = "--dry-run" in sys.argv
    roster = json.load(open(os.path.join(HERE, "..", "roster.json"), encoding="utf-8"))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, failed = [], []
    for e in roster:
        try:
            doc = fetch(e)
            doc["readAtIso"] = now
            rows.append({"coll": "chars", "id": e["id"], "data": doc, "updated_at": now})
            print("ok   %-10s Lv%-2s %-12s IL %-5s CP %s" % (e["id"], doc["level"], doc["cls"], doc["il"], doc["cp"]))
        except Exception as ex:  # keep the previous numbers for this character
            failed.append(e["id"])
            print("FAIL %-10s %s" % (e["id"], ex))
        time.sleep(0.4)
    if dry:
        print("dry run: %d fetched, %d failed, nothing written" % (len(rows), len(failed)))
        return 0 if rows else 1
    url, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_SERVICE_KEY"]
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
