"""Run once: sends all sample articles to Claude and saves the incidents to data/incidents.json.

Usage:  py preextract.py
"""

from app import extract_incidents, samples_as_text, save_incidents

text = samples_as_text()
print("Sending all sample articles to Claude... (this takes a little while)")
incidents = extract_incidents(text)
total = save_incidents(text, incidents)
print(f"Extracted {len(incidents)} incidents. data/incidents.json now has {total} incidents.")
