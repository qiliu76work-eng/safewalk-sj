"""Builds data/real_incidents.json from San Jose's public Police-Calls-for-Service data.

Run once:            py build_real_data.py
Re-download later:   py build_real_data.py --refresh   (reuses cached address lookups)

Steps: find the newest CSV through the city's CKAN API, keep the last 30 days, downtown
streets near SJSU and safety-relevant call types, then look up coordinates for each block
or intersection with OpenRouteService (max 1 request per second, cached).
"""

import csv
import datetime
import json
import os
import re
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
OUT = DATA / "real_incidents.json"
CKAN = "https://data.sanjoseca.gov/api/3/action/package_show?id=police-calls-for-service-quarterly"
ORS_SEARCH = "https://api.openrouteservice.org/geocode/search"
DAYS = 30
MAX_ROWS = 60
# Downtown San Jose near SJSU (Diridon Station to east of campus)
DOWNTOWN = {"min_lat": 37.322, "max_lat": 37.350, "min_lng": -121.906, "max_lng": -121.870}

# Safety-relevant call types -> our categories. Tier 1 = specific incidents, tier 2 = vaguer calls.
# Left out on purpose: traffic, alarms, admin, indoor family/neighbor/music disturbances,
# welfare checks, and mental-health / "vagrant" calls (not a measure of walking safety).
CALL_TYPES = {
    "assault": (1, ["BATTERY", "ASSAULT", "ASSAULT WITH DEADLY WEAPON", "DISTURBANCE, FIGHT",
                    "DISTURBANCE, WEAPON", "BRANDISHING A WEAPON", "PERSON STABBED", "PERSON SHOT",
                    "FIREARMS DISCHARGED", "BATTERY, SERIOUS INJURY"]),
    "theft": (1, ["THEFT", "GRAND THEFT", "PETTY THEFT", "PETTY THEFT (BROADCAST ONLY)", "ROBBERY",
                  "STRONG ARM ROBBERY", "ARMED ROBBERY", "VEHICLE BURGLARY"]),
    "harassment": (1, ["CRIMINAL THREATS", "INDECENT EXPOSURE", "HATE CRIMES"]),
    "stalking": (1, ["STALKING", "PROWLER"]),
    "other": (2, ["DISTURBANCE", "SUSPICIOUS PERSON", "SUSPICIOUS PERSON W/ WEAPON",
                  "PERSON CALLING FOR HELP", "FEMALE CALLING FOR HELP", "KIDNAPPING"]),
}
TYPE_LOOKUP = {}
for cat, (tier, names) in CALL_TYPES.items():
    for name in names:
        TYPE_LOOKUP[name] = (cat, tier)

# Downtown streets -> highest block number that still counts as downtown
STREETS = {
    r"[NS] (1ST|2ND|3RD|4TH|5TH|6TH|7TH|8TH|9TH|10TH|11TH) ST": 700,
    r"[NS] (MARKET|SAN PEDRO|ALMADEN|AUTUMN|MONTGOMERY|DELMAS|CAHILL) (ST|AV|BLVD)": 700,
    r"[EW] (SANTA CLARA|SAN FERNANDO|SAN CARLOS|SAN SALVADOR|WILLIAM|REED|ST JOHN|ST JAMES|JULIAN) ST": 800,
    r"(PASEO DE SAN ANTONIO|PASEO DE SAN SALVADOR|W PARK AV|PARK AV)": 800,
}

WORDS = {"N": "North", "S": "South", "E": "East", "W": "West", "ST": "Street", "AV": "Avenue",
         "BLVD": "Boulevard", "1ST": "1st", "2ND": "2nd", "3RD": "3rd"}


def base_call_type(raw):
    """'BATTERY (COMBINED EVENT)' -> 'BATTERY'"""
    return re.sub(r"\s*\(COMBINED EVENT\)\s*$", "", " ".join(raw.split()))


def is_downtown_street(street):
    return any(re.fullmatch(p, street) for p in STREETS)


def parse_address(raw):
    """Returns ('block', number, street) / ('intersection', street_a, street_b) / None if not downtown."""
    addr = " ".join(raw.split())
    m = re.fullmatch(r"\[(\d+)\]-\[(\d+)\] (.+)", addr)
    if m:
        lo, street = int(m.group(1)), m.group(3)
        if any(re.fullmatch(p, street) and lo <= mx for p, mx in STREETS.items()):
            return ("block", lo, street)
        return None
    if " & " in addr:
        a, b = addr.split(" & ", 1)
        if is_downtown_street(a) and is_downtown_street(b):
            return ("intersection", a, b)
    return None


def spell(street):
    """'N 6TH ST' -> 'North 6th Street'"""
    return " ".join(WORDS.get(w, w.title() if not w[0].isdigit() else w.lower()) for w in street.split())


def core_name(street):
    """'N 6TH ST' -> '6th', 'E SAN FERNANDO ST' -> 'san fernando' (for checking geocoder labels)"""
    words = street.split()
    if words[0] in ("N", "S", "E", "W"):
        words = words[1:]
    if words[-1] in ("ST", "AV", "BLVD"):
        words = words[:-1]
    return " ".join(words).lower()


def pretty_location(parsed):
    if parsed[0] == "block":
        return f"{parsed[1]} block of {spell(parsed[2])}"
    return f"{spell(parsed[1])} & {spell(parsed[2])}"


def time_of_day(hhmmss):
    h = int(hhmmss[:2])
    return "night" if h < 5 or h >= 21 else "morning" if h < 12 else "afternoon" if h < 17 else "evening"


# ---------- Step 1: download ----------

def download_latest():
    print("Finding the newest Police-Calls-for-Service files through the CKAN API...")
    pkg = json.loads(urllib.request.urlopen(CKAN, timeout=60).read())["result"]
    csvs = [r for r in pkg["resources"] if (r.get("format") or "").upper() == "CSV"]
    csvs.sort(key=lambda r: r["name"])  # "Police Calls 2026 Q4" sorts last
    newest = csvs[-2:]  # the last 30 days can span two quarters
    RAW.mkdir(exist_ok=True)
    files = []
    for r in newest:
        path = RAW / Path(urllib.parse.urlparse(r["url"]).path).name
        print(f"  downloading {r['name']} ...")
        download(r["url"], path)
        files.append((r, path))
    return files


def download(url, path):
    """The city's link redirects to a signed Amazon S3 address that rejects Python's
    downloader (403 Forbidden), so use curl, which is built into Windows 10/11."""
    subprocess.run(["curl", "-sSL", "--fail", "--max-time", "300", "-o", str(path), url], check=True)
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        if f.readline().lstrip().lower().startswith("<!doctype html"):
            sys.exit(f"Download of {url} returned a web page instead of a CSV file.")


def load_rows(files):
    rows = []
    for _, path in files:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows += list(csv.DictReader(f))
    return rows


# ---------- Step 2: filter ----------

def filter_rows(rows, today):
    since = (today - datetime.timedelta(days=DAYS)).isoformat()
    picked = []
    for r in rows:
        if r["OFFENSE_DATE"] < since:
            continue
        ctype = base_call_type(r["CALL_TYPE"])
        if ctype not in TYPE_LOOKUP:
            continue
        parsed = parse_address(r["ADDRESS"])
        if not parsed:
            continue
        cat, tier = TYPE_LOOKUP[ctype]
        picked.append({"row": r, "call_type": ctype, "category": cat, "tier": tier, "parsed": parsed})
    # Specific incidents first, then vaguer calls; newest first within each (two stable sorts)
    picked.sort(key=lambda p: p["row"]["OFFENSE_DATE"] + p["row"]["OFFENSE_TIME"], reverse=True)
    picked.sort(key=lambda p: p["tier"])
    return picked


# ---------- Step 3: geocode ----------

class QuotaExceeded(Exception):
    pass


class LookupFailed(Exception):
    pass


class Geocoder:
    def __init__(self, cache):
        self.cache = cache  # address -> [lat, lng] or None (couldn't place it)
        self.key = os.getenv("ORS_API_KEY")
        self.last = 0.0
        self.lookups = 0

    def _search(self, text):
        wait = 1.0 - (time.time() - self.last)  # at most 1 request per second
        if wait > 0:
            time.sleep(wait)
        params = urllib.parse.urlencode({
            "api_key": self.key, "text": text, "size": 1,
            "boundary.rect.min_lat": 37.28, "boundary.rect.max_lat": 37.40,
            "boundary.rect.min_lon": -121.97, "boundary.rect.max_lon": -121.80,
        })
        self.lookups += 1
        try:
            data = json.loads(urllib.request.urlopen(f"{ORS_SEARCH}?{params}", timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):  # OpenRouteService's free daily/minute limit
                raise QuotaExceeded()
            raise LookupFailed()
        except Exception:
            raise LookupFailed()
        finally:
            self.last = time.time()
        return data["features"][0] if data.get("features") else None

    def locate(self, raw_address, parsed):
        """Returns [lat, lng] or None. Only real answers are cached; failed requests are retried next run."""
        if raw_address in self.cache:
            return self.cache[raw_address]
        point = None
        if parsed[0] == "block":
            # Middle of the block, e.g. [400]-[500] -> 450; accept only an exact address match
            text = f"{parsed[1] + 50} {spell(parsed[2])}, San Jose, CA"
            f = self._search(text)
            if f and f["properties"].get("layer") == "address" and core_name(parsed[2]) in f["properties"]["label"].lower():
                point = f["geometry"]["coordinates"][::-1]
        else:
            # Intersection: accept only if the result names both streets
            f = self._search(f"{spell(parsed[1])} & {spell(parsed[2])}, San Jose, CA")
            label = (f or {}).get("properties", {}).get("label", "").lower()
            if f and core_name(parsed[1]) in label and core_name(parsed[2]) in label:
                point = f["geometry"]["coordinates"][::-1]
        if point and not (DOWNTOWN["min_lat"] <= point[0] <= DOWNTOWN["max_lat"]
                          and DOWNTOWN["min_lng"] <= point[1] <= DOWNTOWN["max_lng"]):
            point = None
        self.cache[raw_address] = point
        return point


def main():
    refresh = "--refresh" in sys.argv
    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else None
    if old and not refresh:
        print(f"{OUT} already exists ({len(old['incidents'])} SJPD calls). Use --refresh to rebuild.")
        return
    if not os.getenv("ORS_API_KEY"):
        sys.exit("ORS_API_KEY is missing from your .env file.")

    today = datetime.date.today()
    files = download_latest()
    rows = load_rows(files)
    print(f"Loaded {len(rows)} calls. Columns: {', '.join(rows[0].keys())}")
    candidates = filter_rows(rows, today)
    print(f"{len(candidates)} safety-relevant calls on downtown streets in the last {DAYS} days.")

    geo = Geocoder(old.get("geocode_cache", {}) if old else {})
    incidents, skipped, complete = [], 0, True
    for c in candidates:
        if len(incidents) >= MAX_ROWS:
            break
        r = c["row"]
        try:
            point = geo.locate(r["ADDRESS"], c["parsed"])
        except QuotaExceeded:
            print("\nOpenRouteService's free lookup limit is used up for now. Saving what we have;"
                  " run 'py build_real_data.py --refresh' tomorrow to fill in the rest.")
            complete = False
            break
        except LookupFailed:
            point = None  # not cached, so it's retried next run
        if not point:
            skipped += 1
            continue
        incidents.append({
            "location_text": pretty_location(c["parsed"]),
            "lat": round(point[0], 6), "lng": round(point[1], 6),
            "date": r["OFFENSE_DATE"], "time_of_day": time_of_day(r["OFFENSE_TIME"]),
            "category": c["category"],
            "summary": f"Call to SJPD: {c['call_type'].capitalize()}.",
            "source": "sjpd", "call_type": c["call_type"], "call_number": r["CALL_NUMBER"],
        })
        print(f"  {len(incidents):2d}. {r['OFFENSE_DATE']} {c['call_type']:28s} {pretty_location(c['parsed'])}")

    OUT.write_text(json.dumps({
        "source": "City of San Jose Open Data - Police-Calls-for-Service (quarterly)",
        "source_files": [r["url"].split("?")[0] for r, _ in files],
        "generated_on": today.isoformat(),
        "window": f"last {DAYS} days",
        "complete": complete,
        "incidents": incidents,
        "geocode_cache": geo.cache,
    }, indent=2), encoding="utf-8")
    print(f"\nSaved {len(incidents)} SJPD calls to {OUT} "
          f"({geo.lookups} address lookups, {skipped} calls skipped because their address couldn't be placed).")


if __name__ == "__main__":
    main()
