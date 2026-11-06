"""SafeWalk SJ - paste a news article, see safety incidents on a map."""

import hashlib
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()  # reads ANTHROPIC_API_KEY from the .env file

app = Flask(__name__)
client = anthropic.Anthropic()

MODEL = "claude-sonnet-5-5"
CATEGORIES = ["harassment", "assault", "theft", "stalking", "other"]

RECORD_INCIDENTS_TOOL = {
    "name": "record_incidents",
    "description": "Record every public-safety incident described in the article. "
    "Call with an empty list if the article contains no safety incidents.",
    "strict": True,  # guarantees the output matches this schema exactly
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["incidents"],
        "properties": {
            "incidents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["location_text", "lat", "lng", "date",
                                 "time_of_day", "category", "summary"],
                    "properties": {
                        "location_text": {
                            "type": "string",
                            "description": "Location as described in the article. "
                            "If it is vague, say so, e.g. 'somewhere near downtown (vague)'.",
                        },
                        "lat": {"type": "number", "description": "Best-estimate latitude."},
                        "lng": {"type": "number", "description": "Best-estimate longitude."},
                        "date": {
                            "type": "string",
                            "description": "Date as YYYY-MM-DD if known, otherwise 'unknown'.",
                        },
                        "time_of_day": {
                            "type": "string",
                            "enum": ["morning", "afternoon", "evening", "night", "unknown"],
                        },
                        "category": {"type": "string", "enum": CATEGORIES},
                        "summary": {
                            "type": "string",
                            "description": "One or two neutral sentences. No personal names.",
                        },
                    },
                },
            }
        },
    },
}

SYSTEM_PROMPT = """You extract public-safety incidents from local news about downtown San Jose, CA.
Always respond by calling the record_incidents tool exactly once.

Rules:
- Record every distinct safety incident. Ignore content that is not about safety (events, openings, etc.).
- NEVER include personal names (victims, suspects, witnesses, officers, spokespeople) in any field.
  Use neutral descriptions like "a woman", "a man", "a student" instead.
- Give your best-estimate coordinates using known landmarks (e.g. SJSU is about 37.3352, -121.8811).
  For vague locations, pick the approximate center of the described area and mark location_text as vague.
- Category must be one of: harassment, assault, theft, stalking, other."""


def extract_incidents(article_text):
    # Note: Claude Sonnet 5.5 does not allow *forcing* a tool via tool_choice,
    # so we use tool_choice "auto" + a clear instruction + strict schema.
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[RECORD_INCIDENTS_TOOL],
        tool_choice={"type": "auto"},
        # If Claude declines for a safety reason, retry on a recommended fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": f"<article>\n{article_text}\n</article>"}],
    )

    if response.stop_reason == "refusal":
        raise ValueError("Claude declined to process this article.")

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_incidents":
            return block.input["incidents"]

    raise ValueError("Claude did not return any incidents. Please try again.")


# ---------------- Saved incidents (data/incidents.json) ----------------

DATA_DIR = Path(__file__).parent / "data"
INCIDENTS_PATH = DATA_DIR / "incidents.json"


def load_saved_incidents():
    if not INCIDENTS_PATH.exists():
        return []
    return json.loads(INCIDENTS_PATH.read_text(encoding="utf-8"))


def article_id(text):
    """Same article text (ignoring spacing/line breaks) -> same id."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:12]


def save_incidents(text, incidents):
    """Save incidents from one article. Re-extracting the same article replaces
    its old incidents instead of adding duplicates. Returns the new total."""
    source = article_id(text)
    saved = [inc for inc in load_saved_incidents() if inc.get("source") != source]
    saved += [{**inc, "source": source} for inc in incidents]
    tmp = INCIDENTS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    tmp.replace(INCIDENTS_PATH)  # write-then-rename so the file is never half-written
    return len(saved)


def samples_as_text():
    """All sample articles as one text, in the same format the feed page's Load samples uses."""
    samples = json.loads((DATA_DIR / "sample_articles.json").read_text(encoding="utf-8"))
    return "\n\n".join(f"--- {s['headline']} ---\n{s['body']}" for s in samples)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/feed")
def feed():
    return render_template("feed.html")


@app.route("/api/incidents")
def api_incidents():
    return jsonify(incidents=load_saved_incidents())


@app.route("/api/extract", methods=["POST"])
def api_extract():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="Please paste an article first."), 400
    try:
        incidents = extract_incidents(text)
        total = save_incidents(text, incidents)
    except anthropic.AuthenticationError:
        return jsonify(error="Invalid or missing API key. Check your .env file."), 401
    except anthropic.RateLimitError:
        return jsonify(error="Rate limited by the Claude API. Wait a moment and retry."), 429
    except anthropic.APIConnectionError:
        return jsonify(error="Could not reach the Claude API. Check your internet."), 502
    except anthropic.APIStatusError as e:
        return jsonify(error=f"Claude API error ({e.status_code}): {e.message}"), 502
    except ValueError as e:
        return jsonify(error=str(e)), 502
    return jsonify(incidents=incidents, total_saved=total)


# ---------------- Plan my walk ----------------

ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"
NEAR_ROUTE_METERS = 100

ADVISOR_PROMPT = """You are a calm, practical walking-safety advisor for SJSU students.
A student is glancing at a phone, so keep it short and scannable.
Always respond by calling the give_walk_advice tool exactly once.

- verdict: "green" = take the safer route; "yellow" = either route is fine, just stay aware;
  "red" = consider another option (a ride, waiting for a friend, a different time).
  Use "red" only when several recent incidents sit right on the only practical route at a similar time of day.
  If there is only one route, choose "yellow" or "red".
- headline: at most 8 words, e.g. "Take the green route" or "Either route works tonight".
- reasons: at most 3 bullets, at most 12 words each, only about specific incidents on or near the routes
  (what happened, where, and roughly when). Use an empty list if there are none.
- tip: one practical sentence (for example: stick to busier, well-lit streets, walk with a friend,
  or use the SJSU campus escort service).
- details: 3 to 5 plain sentences comparing the routes and naming the incidents that made the difference.

Tone: calm and confident, never alarming. Never exaggerate risk and never label whole neighborhoods
as dangerous. Plain text only, no markdown."""

WALK_ADVICE_TOOL = {
    "name": "give_walk_advice",
    "description": "Give the student short, scannable walking-safety advice for the planned walk.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "headline", "reasons", "tip", "details"],
        "properties": {
            "verdict": {"type": "string", "enum": ["green", "yellow", "red"]},
            "headline": {"type": "string", "description": "At most 8 words."},
            "reasons": {
                "type": "array",
                "description": "At most 3 bullets, at most 12 words each, about incidents on or near the routes.",
                "items": {"type": "string"},
            },
            "tip": {"type": "string", "description": "One practical sentence."},
            "details": {"type": "string", "description": "3 to 5 plain sentences."},
        },
    },
}


def fallback_advice(details):
    """Used when Claude can't give structured advice, so the page always gets the same shape."""
    return {"verdict": "yellow", "headline": "Compare the routes on the map",
            "reasons": [], "tip": "Stick to busier, well-lit streets and stay aware.", "details": details}


def get_ors_route(start, end, avoid_polygons=None):
    """Ask OpenRouteService for a walking route. start/end are [lat, lng]."""
    key = os.getenv("ORS_API_KEY")
    if not key:
        raise ValueError("ORS_API_KEY is missing from your .env file.")
    body = {"coordinates": [[start[1], start[0]], [end[1], end[0]]]}  # ORS wants [lng, lat]
    if avoid_polygons:
        body["options"] = {"avoid_polygons": avoid_polygons}
    req = urllib.request.Request(
        ORS_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (401, 403):
            raise ValueError("OpenRouteService rejected the ORS_API_KEY. Check your .env file.")
        raise ValueError(f"OpenRouteService error ({e.code}): {detail}")
    except urllib.error.URLError:
        raise ValueError("Could not reach OpenRouteService. Check your internet.")
    feature = data["features"][0]
    return {
        "coords": [[lat, lng] for lng, lat in feature["geometry"]["coordinates"]],  # back to [lat, lng]
        "minutes": round(feature["properties"]["summary"]["duration"] / 60),
        "meters": round(feature["properties"]["summary"]["distance"]),
    }


ORS_AUTOCOMPLETE_URL = "https://api.openrouteservice.org/geocode/autocomplete"
SAN_JOSE_AREA = {  # focus on SJSU, only search inside this box
    "focus.point.lat": 37.3352, "focus.point.lon": -121.8811,
    "boundary.rect.min_lat": 37.28, "boundary.rect.max_lat": 37.40,
    "boundary.rect.min_lon": -121.97, "boundary.rect.max_lon": -121.80,
}


def geocode_autocomplete(text):
    """Place suggestions for a search box. Returns up to 5 {label, lat, lng}."""
    key = os.getenv("ORS_API_KEY")
    if not key:
        raise ValueError("ORS_API_KEY is missing from your .env file.")
    params = urllib.parse.urlencode({"api_key": key, "text": text, "size": 5, **SAN_JOSE_AREA})
    try:
        with urllib.request.urlopen(f"{ORS_AUTOCOMPLETE_URL}?{params}", timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ValueError("OpenRouteService rejected the ORS_API_KEY. Check your .env file.")
        raise ValueError(f"Search error ({e.code}). Please try again.")
    except urllib.error.URLError:
        raise ValueError("Could not reach the search service. Check your internet.")
    return [
        {"label": f["properties"].get("label") or f["properties"].get("name", "Unknown place"),
         "lat": f["geometry"]["coordinates"][1], "lng": f["geometry"]["coordinates"][0]}
        for f in data.get("features", [])[:5]
    ]


@app.route("/api/geocode")
def api_geocode():
    text = request.args.get("q", "").strip()
    if len(text) < 2:
        return jsonify(results=[])
    try:
        return jsonify(results=geocode_autocomplete(text))
    except ValueError as e:
        return jsonify(error=str(e)), 502


def distance_to_route_m(point, coords):
    """Approximate distance in meters from a [lat, lng] point to a route line."""
    lat0 = math.radians(point[0])

    def to_xy(p):  # flat-map approximation, accurate enough at city scale
        return ((p[1] - point[1]) * 111320 * math.cos(lat0), (p[0] - point[0]) * 110540)

    best = float("inf")
    pts = [to_xy(p) for p in coords]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        dx, dy = x2 - x1, y2 - y1
        seg = dx * dx + dy * dy
        t = 0 if seg == 0 else max(0, min(1, -(x1 * dx + y1 * dy) / seg))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def incidents_near(route, incidents):
    return [
        {k: inc[k] for k in ("category", "summary", "location_text", "date", "time_of_day")}
        for inc in incidents
        if distance_to_route_m([inc["lat"], inc["lng"]], route["coords"]) <= NEAR_ROUTE_METERS
    ]


def ask_advisor(normal, safer, time_of_day, note):
    facts = {
        "planned_walking_time": time_of_day,
        "normal_route": {"walking_minutes": normal["minutes"], "incidents_within_100m": normal["nearby"]},
    }
    if safer:
        facts["safer_route"] = {"walking_minutes": safer["minutes"], "incidents_within_100m": safer["nearby"]}
    else:
        facts["safer_route"] = None
        facts["note"] = note
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=ADVISOR_PROMPT,
        tools=[WALK_ADVICE_TOOL],
        tool_choice={"type": "auto"},  # Sonnet 5.5 can't force a tool; the prompt asks for it instead
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": "Here are the route facts:\n" + json.dumps(facts, indent=2)}],
    )
    if response.stop_reason == "refusal":
        return fallback_advice("The advisor couldn't give advice for this walk. Please compare the routes on the map.")
    for block in response.content:
        if block.type == "tool_use" and block.name == "give_walk_advice":
            advice = dict(block.input)
            advice["reasons"] = advice["reasons"][:3]  # the schema can't cap the list, so trim here
            return advice
    # Claude answered in plain text instead of the tool: still show it
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return fallback_advice(text or "The advisor didn't return advice. Please compare the routes on the map.")


@app.route("/api/route", methods=["POST"])
def api_route():
    data = request.get_json(silent=True) or {}
    start, end = data.get("start"), data.get("end")
    incidents = data.get("incidents", [])
    red_squares = data.get("red_squares", [])  # list of polygon rings, [[lng, lat], ...]
    if not start or not end:
        return jsonify(error="Click a start point and an end point first."), 400

    try:
        normal = get_ors_route(start, end)
    except ValueError as e:
        return jsonify(error=str(e)), 502

    # A route can't avoid a zone it starts or ends in, so skip those squares
    def contains(ring, pt):
        lngs, lats = [p[0] for p in ring], [p[1] for p in ring]
        return min(lats) <= pt[0] <= max(lats) and min(lngs) <= pt[1] <= max(lngs)
    red_squares = [sq for sq in red_squares if not contains(sq, start) and not contains(sq, end)]

    safer, note = None, None
    if not red_squares:
        note = "There are no red zones on the map, so there was nothing to avoid."
    else:
        try:
            safer = get_ors_route(start, end, {"type": "MultiPolygon",
                                               "coordinates": [[ring] for ring in red_squares]})
            if safer["coords"] == normal["coords"]:
                safer, note = None, "The route that avoids red zones is the same as the normal route."
        except ValueError:
            note = ("Couldn't find a route that avoids the red zones "
                    "(for example, if the start or end point is inside one).")

    normal["nearby"] = incidents_near(normal, incidents)
    if safer:
        safer["nearby"] = incidents_near(safer, incidents)

    try:
        advice = ask_advisor(normal, safer, data.get("time_of_day", "unknown"), note)
    except anthropic.APIError:
        advice = fallback_advice("The safety advisor is unavailable right now (Claude API error). The routes are still shown.")

    return jsonify(normal=normal, safer=safer, note=note, advice=advice)


@app.route("/api/samples")
def api_samples():
    path = Path(__file__).parent / "data" / "sample_articles.json"
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    app.run(debug=True)
