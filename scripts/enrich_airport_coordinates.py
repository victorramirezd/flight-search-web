#!/usr/bin/env python3
"""Add coordinates to the airport/city options used by the web UI."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
AIRPORTS_JSON = ROOT / "data" / "airports.json"
OURAIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"

CITY_COORDINATES = {
    "BJS": (39.9042, 116.4074),
    "BUE": (-34.6037, -58.3816),
    "CHI": (41.8781, -87.6298),
    "LON": (51.5072, -0.1276),
    "MIL": (45.4642, 9.19),
    "MOW": (55.7558, 37.6173),
    "NYC": (40.7128, -74.006),
    "OSA": (34.6937, 135.5023),
    "PAR": (48.8566, 2.3522),
    "RIO": (-22.9068, -43.1729),
    "ROM": (41.9028, 12.4964),
    "SAO": (-23.5558, -46.6396),
    "SEL": (37.5665, 126.978),
    "SHA": (31.2304, 121.4737),
    "STO": (59.3293, 18.0686),
    "TYO": (35.6762, 139.6503),
    "WAS": (38.9072, -77.0369),
    "YMQ": (45.5019, -73.5674),
    "YTO": (43.6532, -79.3832),
}

AIRPORT_COORDINATE_OVERRIDES = {
    "CVT": (52.3697, -1.4797),
    "IEV": (50.4019, 30.4519),
    "KSS": (11.333, -5.7),
    "WIT": (-22.2417, 118.3356),
}


def load_ourairports_coordinates() -> dict[str, tuple[float, float]]:
    with urlopen(OURAIRPORTS_CSV_URL, timeout=30) as response:
        rows = csv.DictReader(line.decode("utf-8") for line in response)
        coordinates = {}
        for row in rows:
            code = row.get("iata_code", "").strip().upper()
            latitude = row.get("latitude_deg", "").strip()
            longitude = row.get("longitude_deg", "").strip()
            if not code or not latitude or not longitude:
                continue
            coordinates[code] = (float(latitude), float(longitude))
        return coordinates


def main() -> None:
    airports = json.loads(AIRPORTS_JSON.read_text(encoding="utf-8"))
    coordinates_by_code = load_ourairports_coordinates()

    enriched = 0
    missing = []
    for airport in airports:
        code = str(airport.get("code", "")).upper()
        coordinates = (
            CITY_COORDINATES.get(code)
            if airport.get("type") == "city"
            else AIRPORT_COORDINATE_OVERRIDES.get(code) or coordinates_by_code.get(code)
        )
        if not coordinates:
            missing.append(code)
            airport.pop("latitude", None)
            airport.pop("longitude", None)
            continue

        latitude, longitude = coordinates
        airport["latitude"] = round(latitude, 6)
        airport["longitude"] = round(longitude, 6)
        enriched += 1

    AIRPORTS_JSON.write_text(json.dumps(airports, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Enriched {enriched:,} of {len(airports):,} records with coordinates.")
    if missing:
        print(f"{len(missing):,} records still have no coordinates.")


if __name__ == "__main__":
    main()
