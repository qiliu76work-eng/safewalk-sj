# SafeWalk

SafeWalk plans safer walking routes for women, steering around safety zones built from real city police data and local news that Claude reads, then explains the route in plain language.

**Demo video:** https://youtu.be/Pm12J6SZUxg

## The problem

SJSU students often walk through downtown San José at night, from the library to light rail, Diridon Station or a late dinner. The information that could help them is scattered across news sites, police data portals and word of mouth. And crime maps show dots, not answers: they don't tell you which way to walk home right now.

## What it does

- **From / To search** with live suggestions, a "Use my location" button, a swap button, quick picks for common destinations (Santa Clara St light rail, Diridon Station, San Pedro Square, SAP Center), or tap the map.
- **When?** "Leave now" uses the current time, or "Pick a time" lets you plan ahead. The advice changes with the time of day.
- **Two routes on the map:** gray is the fastest walking route, green is a safer route that steers around red safety zones, each with its walking time.
- **Claude's advice card**, made to be read at a glance on a phone: a verdict badge (safer route / stay aware / consider another option), a headline of 8 words or fewer, up to 3 reasons tied to specific incidents near the routes, 1 practical tip, and a tap-to-open section with the full explanation.
- **Safety zones:** downtown is split into squares about 150 m wide, colored by the number of reported incidents in each: green (none reported), yellow (1–2), red (3 or more). Green squares are drawn only next to squares with reports.
- **Show: Real city data / Demo data / Both** switches between the two data layers. The zones and route planning follow whatever is shown.

## How Claude is used

The app uses the Claude API (`claude-sonnet-5-5`) for two jobs:

1. **Reading the news into structured incidents.** On the admin page (`/feed`), a news article goes to Claude with a `record_incidents` tool that has a strict schema. Claude returns each safety incident as `{location_text, lat, lng, date, time_of_day, category, summary}`, with category one of harassment, assault, theft, stalking or other. It's told never to include personal names (victims, suspects, witnesses or officers) and to skip stories that aren't about safety, like a library changing its hours or an art walk.
2. **Comparing routes and writing the advice card.** For a planned walk, Claude gets both routes' walking times, the incidents within about 100 m of each route, and the time of day. It answers through a `give_walk_advice` tool with a verdict, headline, reasons, tip and details. Its instructions keep the tone calm and practical, rule out exaggerating risk, and tell it to describe police data as "calls to police", not crimes.

## Data

- **Real data: City of San José Open Data, "Police-Calls-for-Service-Quarterly", file *Police Calls 2026 Q4*.**
  - URL: https://data.sanjoseca.gov/dataset/c414a023-8e2c-4ed0-a0e0-2731913161a1/resource/ebddb75e-ee46-492c-8200-1cbff7ea58bf/download/policecalls2026-q4.csv
  - **On the map: 57 calls at 24 locations, from Oct 7 to Nov 3, 2026.**
  - Filters: the last 30 days, downtown streets near SJSU, and safety-relevant call types (assault, robbery and theft, fights, weapon disturbances, threats, indecent exposure, stalking, disturbance, suspicious person). Traffic, alarms, admin calls, indoor family or neighbor disturbances, welfare checks and mental-health calls are left out.
  - The city publishes block-level addresses, not coordinates, so `build_real_data.py` looks each one up with OpenRouteService. It keeps only exact address matches and intersections where the result names both streets.
  - This layer is **partial**: 436 calls matched the filters, but the free geocoding quota ran out, so only the 57 calls whose addresses could be placed are shown.
- **Calls for service are not confirmed crimes.** They're calls to police, and the app labels them that way.
- **Locations are approximate**, to the block or intersection.
- **Demo data is synthetic:** 18 made-up local-news briefs about real downtown landmarks (`data/sample_articles.json`). Claude extracted 16 incidents from them; 2 of the briefs aren't about safety. No real people are involved.

## Responsible design

- **Green means "no recent reports", not "safe".** The legend says so: *"Green = no recent reports, not a guarantee of safety."*
- **Small squares, not neighborhood labels.** Zones are about 150 m wide and count reports. The app never labels a neighborhood as dangerous, and Claude is told not to.
- **No names are stored.** The extraction tool has no field for names, and Claude is told to leave them out.
- **Honest when there's no better option.** If no safer route exists (no red zones nearby, the detour fails, or both routes come out the same), the app shows one route and says why.
- **Calm tone.** Verdict colors are soft, and Claude is told to be calm and confident, never alarming. A red verdict is reserved for several recent incidents right on the only practical route.

## Known limitations and next steps

- **Relative zones:** zones use fixed counts (0 / 1–2 / 3+). They should adapt to the area and to how much data there is.
- **Recency and time-of-day weighting:** a fight last night at 11 PM should count more for a 10 PM walk than a theft three weeks ago at noon.
- **Detour cap with risk-reduction advice:** when the safer route is much longer, suggest ways to lower risk on the direct route instead of a long detour.
- **Start walk:** check-ins along the way and live location sharing with a friend.
- **Automatic sources:** feed in local news and the SJSU crime log automatically instead of pasting articles on the admin page.
- **More cities** beyond San José.
- **SMS** for people without a smartphone or data plan.
- **A safety layer for map apps** people already use.

## How to run it (Windows)

You need Python 3.10 or newer, an [Anthropic API key](https://platform.claude.com) and a free [OpenRouteService API key](https://openrouteservice.org/dev/#/signup).

1. Download or clone the repo and open a terminal in its folder:
   ```
   git clone https://github.com/qiliu76work-eng/safewalk-sj.git
   cd safewalk-sj
   ```
2. Install the packages:
   ```
   py -m pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in your keys:
   ```
   ANTHROPIC_API_KEY=
   ORS_API_KEY=
   ```
4. Start the app:
   ```
   py app.py
   ```
5. Open **http://127.0.0.1:5000**. Incidents and zones load right away. Type a destination, then tap **Plan my walk**.
6. **Admin page:** **http://127.0.0.1:5000/feed** ("Behind the scenes: Claude reads the news"). Paste an article or click **Load samples**, then **Extract incidents**. The results are saved to `data/incidents.json` and appear on the main map after a refresh.

Both data files come with the repo. Rebuilding them is optional:
- `py preextract.py` re-extracts the sample articles with Claude.
- `py build_real_data.py --refresh` downloads the latest police calls and looks up any addresses that haven't been found yet.

## Built with

- [Claude API](https://platform.claude.com) (Claude Sonnet 5.5, tool use with strict schemas)
- [Claude Code](https://claude.com/claude-code)
- [Flask](https://flask.palletsprojects.com/)
- [Leaflet](https://leafletjs.com/) + [OpenStreetMap](https://www.openstreetmap.org/)
- [OpenRouteService](https://openrouteservice.org/) (walking directions and geocoding)
- [City of San José Open Data](https://data.sanjoseca.gov/)
