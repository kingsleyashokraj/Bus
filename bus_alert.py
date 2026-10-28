#!/usr/bin/env python3
"""Cincinnati Metro bus alert.

Watches Metro's live GTFS-Realtime feed and sends a push notification to your
phone when your bus is about to reach your stop (walk time + buffer away).

Usage:
  python bus_alert.py                 # normal run (used by the scheduler)
  python bus_alert.py --find-stop     # show which stop_id it picked for your stop
  python bus_alert.py --test-notify   # send a test push to your phone
  python bus_alert.py --dry-run       # print alerts instead of sending them
  python bus_alert.py --force         # ignore the weekday / start-time check
"""
import argparse
import csv
import io
import math
import os
import sys
import time
import zipfile
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

TZ = ZoneInfo("America/New_York")
# Metro's server rejects the default python-requests User-Agent (403).
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
RT = "https://tmgtfsprd.sorttrpcloud.com/TMGTFSRealTimeWebService"


def env(name, default):
    """Environment variable, treating empty strings as unset."""
    return os.getenv(name) or default


TRIP_URL = env("TRIP_URL", RT + "/tripupdate/tripupdates.pb")
VEH_URL = env("VEHICLE_URL", RT + "/vehicle/vehiclepositions.pb")
GTFS_URL = env("GTFS_URL", "https://www.go-metro.com/uploads/GTFS/google_transit_info.zip")

ROUTE = env("ROUTE", "3X")
STOP_NAME = env("STOP_NAME", "Montgomery,Kemper")   # words that must all appear in the stop name
STOP_ID = env("STOP_ID", "")                        # optional: pin an exact stop_id
HEADSIGN = env("HEADSIGN", "downtown")              # picks the inbound (to-work) side of the road

WALK_MIN = float(env("WALK_MIN", "8"))              # minutes to reach the stop
BUFFER_MIN = float(env("BUFFER_MIN", "3"))          # extra cushion
BUS_FROM = env("BUS_FROM", "06:30")                 # only alert buses scheduled at your stop
BUS_TO = env("BUS_TO", "08:30")                     #   between these local times
START_LOCAL = env("START_LOCAL", "06:00")           # when the scheduler launches the script
POLL_SEC = int(env("POLL_SEC", "30"))
MAX_ALERTS = int(env("MAX_ALERTS", "1"))            # stop after this many alerts per morning
FALLBACK_MILES = float(env("FALLBACK_MILES", "1.5"))  # used only if no arrival estimate exists

NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh")

# Weather (free Open-Meteo forecast, no key needed). Defaults to Montgomery, OH.
WEATHER_URL = env("WEATHER_URL", "https://api.open-meteo.com/v1/forecast")
LAT = env("LAT", "39.2284")
LON = env("LON", "-84.3544")
WEATHER_UNTIL = env("WEATHER_UNTIL", "18:00")       # look at the forecast from now until this time (ride home)
RAIN_PROB = float(env("RAIN_PROB", "40"))           # % chance of precipitation that means "take an umbrella"
SNOW_MIN_IN = float(env("SNOW_MIN_IN", "0.1"))      # inches of snow that means "snow jacket"
WEATHER_ALWAYS = bool(os.getenv("WEATHER_ALWAYS"))  # set to get a daily "no gear needed" note too
SNOW_CODES = {71, 73, 75, 77, 85, 86}               # WMO weather codes for snow
WET_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def log(*a):
    print(dt.datetime.now(TZ).strftime("%H:%M:%S"), *a, flush=True)


def secs(hhmm):
    h, m = hhmm.split(":")[:2]
    return int(h) * 3600 + int(m) * 60


def gtfs_secs(t):
    h, m, s = (int(x) for x in t.strip().split(":"))
    return h * 3600 + m * 60 + s


def miles(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- notifications
def notify(title, body, dry=False, urgent=True, tags="bus"):
    if dry:
        log(f"[dry-run] {title} | {body}")
        return
    sent = False
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        r = requests.post(f"{NTFY_SERVER}/{topic}", data=body.encode("utf-8"),
                          headers={"Title": title, "Priority": "5" if urgent else "3",
                                   "Tags": tags}, timeout=15)
        r.raise_for_status()
        sent = True
    if os.getenv("PUSHOVER_TOKEN") and os.getenv("PUSHOVER_USER"):
        # priority 1 = high: bypasses Pushover's quiet hours
        r = requests.post("https://api.pushover.net/1/messages.json", timeout=15, data={
            "token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
            "title": title, "message": body, "priority": 1 if urgent else 0})
        r.raise_for_status()
        sent = True
    if not sent:
        raise SystemExit("No notification channel configured. Set NTFY_TOPIC "
                         "(or PUSHOVER_TOKEN and PUSHOVER_USER).")
    log("sent:", title, "|", body)


# ---------------------------------------------------------------- weather
def hour_label(h):
    return f"{(h % 12) or 12}{'am' if h < 12 else 'pm'}"


def weather_advice(now):
    """Return (title, body, tags) for today's forecast, or None if no gear is needed
    (unless WEATHER_ALWAYS is set). Covers the hours from now until WEATHER_UNTIL."""
    r = requests.get(WEATHER_URL, timeout=20, params={
        "latitude": LAT, "longitude": LON, "timezone": "America/New_York", "forecast_days": 1,
        "hourly": "temperature_2m,precipitation_probability,precipitation,snowfall,weather_code",
        "temperature_unit": "fahrenheit", "precipitation_unit": "inch"})
    r.raise_for_status()
    h = r.json()["hourly"]
    today = now.strftime("%Y-%m-%d")
    last = int(secs(WEATHER_UNTIL) // 3600)
    idx = [i for i, t in enumerate(h["time"])
           if t[:10] == today and now.hour <= int(t[11:13]) <= last]
    if not idx:
        raise RuntimeError("forecast has no hours in the requested window")

    def col(name):
        return [h[name][i] for i in idx]
    prob = [p or 0 for p in col("precipitation_probability")]
    precip = sum(p or 0 for p in col("precipitation"))
    snow = sum(s or 0 for s in col("snowfall"))
    codes = [c for c in col("weather_code") if c is not None]
    temps = [t for t in col("temperature_2m") if t is not None]
    hours = [int(h["time"][i][11:13]) for i in idx]

    snowy = snow >= SNOW_MIN_IN or any(c in SNOW_CODES for c in codes)
    wet = max(prob, default=0) >= RAIN_PROB or precip >= 0.05 or any(c in WET_CODES for c in codes)
    temp_txt = f"{round(min(temps))}-{round(max(temps))}F" if temps else ""
    until = hour_label(last)

    def first_hour(hit):
        for k, hr in enumerate(hours):
            if hit(prob[k], col("weather_code")[k]):
                return hour_label(hr)
        return hour_label(hours[0])

    if snowy:
        start = first_hour(lambda p, c: c in SNOW_CODES)
        return ("Wear your snow jacket",
                f"Snow expected today (about {snow:.1f} in, starting around {start}). "
                f"{temp_txt} through {until}.", "snowflake")
    if wet:
        start = first_hour(lambda p, c: p >= RAIN_PROB or c in WET_CODES)
        return ("Take an umbrella",
                f"Rain likely today ({max(prob, default=0):.0f}% chance, starting around {start}). "
                f"{temp_txt} through {until}.", "umbrella")
    if WEATHER_ALWAYS:
        return ("No rain or snow today", f"Dry through {until}. {temp_txt}.", "sunny")
    return None


def send_weather(now, dry=False):
    """Never let a weather problem get in the way of the bus alert."""
    if os.getenv("NO_WEATHER"):
        return
    try:
        adv = weather_advice(now)
        if adv:
            notify(adv[0], adv[1], dry=dry, urgent=False, tags=adv[2])
        else:
            log("weather: no umbrella or snow jacket needed")
    except Exception as ex:
        log("weather check failed:", ex)


# ---------------------------------------------------------------- static GTFS
def load_static():
    """Return (chosen_stop, candidates). Each stop dict has id, name, lat, lon, trips.
    trips maps trip_id -> (scheduled seconds after midnight at the stop, stop_sequence)."""
    r = requests.get(GTFS_URL, headers=UA, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    want = ROUTE.strip().lower()
    route_ids = {x["route_id"] for x in rows("routes.txt")
                 if x.get("route_short_name", "").strip().lower() == want
                 or x["route_id"].strip().lower() == want}
    if not route_ids:
        raise SystemExit(f"Route {ROUTE!r} not found in the schedule data.")

    headsigns = {t["trip_id"]: t.get("trip_headsign", "") for t in rows("trips.txt")
                 if t["route_id"] in route_ids}

    words = [w.strip().lower() for w in STOP_NAME.split(",") if w.strip()]
    stops = {}
    for s in rows("stops.txt"):
        name = s["stop_name"].lower()
        if (STOP_ID and s["stop_id"] == STOP_ID) or (not STOP_ID and all(w in name for w in words)):
            stops[s["stop_id"]] = {"id": s["stop_id"], "name": s["stop_name"],
                                   "lat": float(s["stop_lat"]), "lon": float(s["stop_lon"]),
                                   "trips": {}, "headsigns": {}}
    if not stops:
        raise SystemExit(f"No stop matching {STOP_ID or STOP_NAME!r} found.")

    for st in rows("stop_times.txt"):
        sid = st["stop_id"]
        if sid in stops and st["trip_id"] in headsigns:
            t = st.get("arrival_time") or st.get("departure_time")
            if not t:
                continue
            stops[sid]["trips"][st["trip_id"]] = (gtfs_secs(t), int(st["stop_sequence"]))
            hs = headsigns[st["trip_id"]]
            stops[sid]["headsigns"][hs] = stops[sid]["headsigns"].get(hs, 0) + 1

    cands = [s for s in stops.values() if s["trips"]]
    if not cands:
        raise SystemExit(f"Route {ROUTE} does not serve any stop matching {STOP_NAME!r}.")
    if STOP_ID or len(cands) == 1:
        return cands[0], cands

    def inbound(s):
        return sum(n for h, n in s["headsigns"].items() if HEADSIGN.lower() in h.lower())
    cands.sort(key=lambda s: (inbound(s), len(s["trips"])), reverse=True)
    if inbound(cands[0]) == 0:
        log("WARNING: could not tell which side of the road is inbound; picked the busiest stop. "
            "Run --find-stop and set STOP_ID if this is wrong.")
    return cands[0], cands


# ---------------------------------------------------------------- realtime
def fetch(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    m = gtfs_realtime_pb2.FeedMessage()
    m.ParseFromString(r.content)
    return m


def sched_epoch(start_date, s):
    if start_date:
        base = dt.datetime.strptime(start_date, "%Y%m%d").replace(tzinfo=TZ)
    else:
        base = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return base.timestamp() + s


def estimates(stop, now):
    """Return {trip_id: (minutes_away, how)} for buses that have not reached the stop yet."""
    out = {}
    lo, hi = secs(BUS_FROM), secs(BUS_TO)
    trips = {t: v for t, v in stop["trips"].items() if lo <= v[0] <= hi}
    if not trips:
        return out

    for e in fetch(TRIP_URL).entity:
        if not e.HasField("trip_update"):
            continue
        tu = e.trip_update
        tid = tu.trip.trip_id
        if tid not in trips:
            continue
        sched_s, _seq = trips[tid]
        eta = None
        delay = None
        for u in tu.stop_time_update:
            ev = u.arrival if u.HasField("arrival") else (u.departure if u.HasField("departure") else None)
            if ev is None:
                continue
            if u.stop_id == stop["id"] and ev.time:
                eta = ev.time
                break
            if delay is None and ev.HasField("delay"):
                delay = ev.delay
        if eta is None and delay is not None:
            eta = sched_epoch(tu.trip.start_date, sched_s) + delay
        if eta is not None:
            out[tid] = ((eta - now) / 60.0, "eta")

    # Fallback for buses with no arrival estimate: straight-line distance.
    try:
        for e in fetch(VEH_URL).entity:
            if not e.HasField("vehicle"):
                continue
            v = e.vehicle
            tid = v.trip.trip_id
            if tid not in trips or tid in out:
                continue
            if v.current_stop_sequence and v.current_stop_sequence > trips[tid][1]:
                continue  # already past your stop
            d = miles(v.position.latitude, v.position.longitude, stop["lat"], stop["lon"])
            if d <= FALLBACK_MILES:
                out[tid] = (d, "dist")
    except Exception as ex:  # fallback only; never block the main path
        log("vehicle feed problem:", ex)
    return out


# ---------------------------------------------------------------- main loop
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--find-stop", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--weather-only", action="store_true",
                    help="send today's umbrella / snow jacket note and exit")
    args = ap.parse_args()

    if args.test_notify:
        notify("Bus alert test", "If you see this, your phone alerts work.", dry=args.dry_run)
        return

    if args.weather_only:
        send_weather(dt.datetime.now(TZ), args.dry_run)
        return

    if args.find_stop:
        stop, cands = load_static()
        for s in cands:
            mark = "  <== chosen" if s["id"] == stop["id"] else ""
            print(f"stop_id={s['id']}  {s['name']}  ({s['lat']}, {s['lon']})  "
                  f"trips={len(s['trips'])}  headsigns={s['headsigns']}{mark}")
        return

    now = dt.datetime.now(TZ)
    if not args.force:
        if now.weekday() >= 5:
            log("weekend, nothing to do")
            return
        cur = now.hour * 3600 + now.minute * 60
        start = secs(START_LOCAL)
        # Two scheduler entries exist (for daylight saving); only the one that fires
        # near START_LOCAL should do the work.
        if not (start - 900 <= cur <= start + 2700):
            log("outside launch window, exiting")
            return

    send_weather(dt.datetime.now(TZ), args.dry_run)   # one gear note per morning

    stop, _ = load_static()
    lead = WALK_MIN + BUFFER_MIN
    log(f"watching route {ROUTE} at '{stop['name']}' (stop_id {stop['id']}); "
        f"alert when bus is <= {lead:.0f} min away")

    end = secs(BUS_TO) + 600
    alerted, sent, errors, warned = set(), 0, 0, False
    while sent < MAX_ALERTS:
        now = dt.datetime.now(TZ)
        if now.hour * 3600 + now.minute * 60 > end:
            log("window over, exiting")
            break
        try:
            est = estimates(stop, time.time())
            errors = 0
            for tid, (val, how) in sorted(est.items(), key=lambda kv: kv[1][0]):
                if tid in alerted:
                    continue
                if how == "eta":
                    if not (0 < val <= lead):
                        continue
                    eta_txt = f"about {max(1, round(val))} min away"
                    late = val < WALK_MIN
                else:
                    eta_txt = f"{val:.1f} mi away"
                    late = False
                alerted.add(tid)
                body = (f"Route {ROUTE} is {eta_txt} from {stop['name']}. "
                        + ("Go NOW, you may need to hurry!" if late else
                           f"Leave now ({WALK_MIN:.0f} min walk)."))
                notify(f"{ROUTE} bus - go to the stop!", body, dry=args.dry_run)
                sent += 1
                if sent >= MAX_ALERTS:
                    break
        except Exception as ex:
            errors += 1
            log("error:", ex)
            if errors >= 8 and not warned:
                warned = True
                try:
                    notify("Bus alert: feed problem",
                           "Can't reach Metro's live data. Check the bus app manually today.",
                           dry=args.dry_run)
                except Exception as ex2:
                    log("could not send warning:", ex2)
        if sent < MAX_ALERTS:
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
