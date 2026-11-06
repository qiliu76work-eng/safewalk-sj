"""SafeWalk SJ - paste a news article, see safety incidents on a map."""

import json
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


@app.route("/api/samples")
def api_samples():
    path = Path(__file__).parent / "data" / "sample_articles.json"
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    app.run(debug=True)
