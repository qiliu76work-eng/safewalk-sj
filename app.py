"""SafeWalk SJ - paste a news article, see safety incidents on a map."""

import json
import math
import os
import urllib.error
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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/extract", methods=["POST"])
def api_extract():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="Please paste an article first."), 400
    try:
        incidents = extract_incidents(text)
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
    return jsonify(incidents=incidents)


# ---------------- Plan my walk ----------------

ORS_URL = "https://api.openrouteservice.org/v2/directions/foot-walking/geojson"
NEAR_ROUTE_METERS = 100

ADVISOR_PROMPT = """You are a calm, practical walking-safety advisor for SJSU students.
In 3 to 5 sentences, compare the two routes, mention the specific incidents that made the difference,
and suggest one practical tip (for example: stick to busier streets, walk with a friend, or use the
campus escort service). Never exaggerate risk and never label whole neighborhoods as dangerous.
Write plain sentences with no headings, bullet points or markdown."""


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
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": "Here are the route facts:\n" + json.dumps(facts, indent=2)}],
    )
    if response.stop_reason == "refusal":
        return "The advisor couldn't give advice for this walk. Please compare the routes on the map."
    return "".join(b.text for b in response.content if b.type == "text").strip()


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
        advice = "The safety advisor is unavailable right now (Claude API error). The routes are still shown."

    return jsonify(normal=normal, safer=safer, note=note, advice=advice)


@app.route("/api/samples")
def api_samples():
    path = Path(__file__).parent / "data" / "sample_articles.json"
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    app.run(debug=True)
