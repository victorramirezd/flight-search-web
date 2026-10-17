#!/usr/bin/env python3
"""Compare the complete local catalog with Duffel v2 reference lists.

Only GET /air/airports and GET /air/cities are used. No flight offer requests
are made. A failed or incomplete download never produces a validation report.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = "https://api.duffel.com"
SOURCES = {"airport": "/air/airports", "city": "/air/cities"}
COUNTRY_NAMES = {
    "argentina": "AR", "brazil": "BR", "canada": "CA", "china": "CN",
    "france": "FR", "italy": "IT", "japan": "JP", "russia": "RU",
    "south korea": "KR", "united kingdom": "GB", "united states": "US",
}


class CatalogError(Exception):
    """Invalid, ambiguous or incomplete reference data; do not publish it."""


def normalized(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())


def country_code(value: object) -> str:
    text = str(value or "").strip()
    return text.upper() if re.fullmatch(r"[A-Za-z]{2}", text) else COUNTRY_NAMES.get(normalized(text), "")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def get_page(session: requests.Session, path: str, params: dict,
             sleep: Callable = time.sleep) -> dict:
    for attempt in range(4):
        try:
            response = session.get(API_ROOT + path, params=params, timeout=(10, 45),
                                   allow_redirects=False)
        except requests.RequestException:
            if attempt == 3:
                raise CatalogError(f"Duffel {path}: network request failed after retries") from None
            sleep(2 ** attempt)
            continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt == 3:
                raise CatalogError(f"Duffel {path}: HTTP {response.status_code} after retries")
            try:
                delay = float(response.headers.get("Retry-After", 2 ** attempt))
            except ValueError:
                delay = 2 ** attempt
            if not math.isfinite(delay) or delay > 60:
                raise CatalogError(f"Duffel {path}: rate limit requires waiting; rerun later")
            sleep(max(delay, 1))
            continue
        if response.status_code != 200:
            # Never log request headers, tokens, or arbitrary response bodies.
            raise CatalogError(f"Duffel {path}: HTTP {response.status_code}; check API access")
        try:
            payload = response.json()
        except ValueError:
            raise CatalogError(f"Duffel {path}: response is not JSON") from None
        if not isinstance(payload, dict):
            raise CatalogError(f"Duffel {path}: response must be an object")
        return payload
    raise CatalogError("Unexpected retry termination")


def fetch_list(session: requests.Session, kind: str, sleep: Callable = time.sleep,
               progress: Callable = print) -> tuple[list[dict], int]:
    path = SOURCES[kind]
    records, seen_ids, seen_cursors = [], set(), set()
    after = None
    pages = 0
    while True:
        params = {"limit": 200}
        if after is not None:
            params["after"] = after
        payload = get_page(session, path, params, sleep)
        data, meta = payload.get("data"), payload.get("meta")
        if not isinstance(data, list) or not isinstance(meta, dict) or "after" not in meta:
            raise CatalogError(f"Duffel {path}: missing data or pagination metadata")
        for record in data:
            if not isinstance(record, dict) or not record.get("id"):
                raise CatalogError(f"Duffel {path}: record without an ID")
            if record["id"] in seen_ids:
                raise CatalogError(f"Duffel {path}: duplicate ID across pages; rerun for a consistent snapshot")
            seen_ids.add(record["id"])
            records.append(record)
        pages += 1
        progress(f"Downloaded {kind} page {pages}: {len(records):,} records", flush=True)
        after = meta["after"]
        if after is None:
            break
        if not isinstance(after, str) or not after or after in seen_cursors or not data:
            raise CatalogError(f"Duffel {path}: invalid or repeating pagination cursor")
        seen_cursors.add(after)
        sleep(1.1)
    if not records:
        raise CatalogError(f"Duffel {path}: empty reference list")
    return records, pages


def index_reference(records: list[dict], kind: str) -> dict[str, list[dict]]:
    index = defaultdict(list)
    for record in records:
        code = record.get("iata_code")
        country = record.get("iata_country_code")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{3}", code):
            raise CatalogError(f"Duffel {kind}: record with invalid IATA code")
        if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
            raise CatalogError(f"Duffel {kind} {code}: invalid country code")
        if not isinstance(record.get("name"), str) or not record["name"].strip():
            raise CatalogError(f"Duffel {kind} {code}: missing name")
        if kind == "city" and not isinstance(record.get("airports"), list):
            raise CatalogError(f"Duffel city {code}: missing airport membership")
        index[code].append(record)
    return dict(index)


def coordinates(record: dict) -> tuple[float, float] | None:
    lat, lon = record.get("latitude"), record.get("longitude")
    if (isinstance(lat, (int, float)) and not isinstance(lat, bool) and
        isinstance(lon, (int, float)) and not isinstance(lon, bool) and
        math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return float(lat), float(lon)
    return None


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    delta = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1, math.sqrt(delta)))


def merged_airports(airports: list[dict], cities: list[dict]) -> list[dict]:
    # Some airports (e.g. MKC) appear only in a city's airport membership.
    # Prefer the top-level record when the same provider ID appears in both.
    by_id = {airport["id"]: airport for airport in airports}
    primary_keys = {(airport.get("iata_code"), airport.get("iata_country_code")) for airport in airports}
    for city in cities:
        for airport in city.get("airports", []):
            if not isinstance(airport, dict) or not airport.get("id"):
                raise CatalogError("Duffel city contains an airport without an ID")
            existing = by_id.get(airport["id"])
            if existing and existing.get("iata_code") != airport.get("iata_code"):
                raise CatalogError("Duffel airport code conflicts between reference lists")
            if not existing and (airport.get("iata_code"), airport.get("iata_country_code")) not in primary_keys:
                by_id[airport["id"]] = airport
    return list(by_id.values())


def compare_catalog(local: list[dict], airports: list[dict], cities: list[dict]) -> tuple[list[dict], list[dict]]:
    indexes = {"airport": index_reference(merged_airports(airports, cities), "airport"),
               "city": index_reference(cities, "city")}
    counts = Counter(place.get("code") for place in local)
    results, proposed = [], []
    for place in local:
        code = str(place.get("code", ""))
        kind = "city" if place.get("type") == "city" else "airport"
        row = {"code": code, "type": kind, "local_name": place.get("name"),
               "local_country": place.get("country"), "status": "", "changes": {}}
        results.append(row)
        if place.get("type") not in {"city", "airport", "large_airport", "medium_airport", "small_airport"}:
            row["status"] = "invalid_local_type"
            continue
        if not re.fullmatch(r"[A-Z]{3}", code):
            row["status"] = "invalid_local_code"
            continue
        if counts[code] > 1:
            row["status"] = "duplicate_local_code"
            continue
        candidates = indexes[kind].get(code, [])
        converted_to_city = False
        if not candidates:
            # A code that Duffel defines only as a city must be labelled as a city,
            # never as the old individual airport. Do not transfer its old geometry.
            if kind == "airport" and code in indexes["city"]:
                candidates = indexes["city"][code]
                converted_to_city = True
            else:
                row["status"] = "type_mismatch" if code in indexes["airport"] else "not_in_duffel"
                continue
        local_country = country_code(place.get("country"))
        if not local_country:
            row["status"] = "unrecognized_local_country"
            continue
        same_country = [record for record in candidates if record["iata_country_code"] == local_country]
        country_exception = False
        if not same_country:
            # Country metadata can be stale or wrong in Duffel. An exact code plus
            # coordinates within 5 km establishes the same physical airport while
            # retaining local country/geography. Far-away code collisions stay out.
            point = coordinates(place)
            nearby = [record for record in candidates if point and coordinates(record)
                      and distance_km(point, coordinates(record)) <= 5]
            if not converted_to_city and len(nearby) == 1:
                same_country = nearby
                country_exception = True
                row["duffel_countries"] = [nearby[0]["iata_country_code"]]
            else:
                row["status"] = "country_mismatch"
                row["duffel_countries"] = sorted({record["iata_country_code"] for record in candidates})
                continue
        if len(same_country) != 1:
            row["status"] = "ambiguous_duffel_code"
            continue
        reference = same_country[0]
        row["duffel_id"] = reference["id"]
        row["duffel_name"] = reference["name"]
        row["duffel_country"] = reference["iata_country_code"]
        row["status"] = ("converted_to_city" if converted_to_city else
                         "confirmed_country_exception" if country_exception else "confirmed")
        new = dict(place)
        effective_kind = "city" if converted_to_city else kind
        point, old_point = coordinates(reference), coordinates(place)
        distant = False
        if effective_kind == "airport" and point and old_point:
            row["coordinate_distance_km"] = round(distance_km(old_point, point), 3)
            distant = row["coordinate_distance_km"] > 100
        if distant:
            row["metadata_warning"] = "Duffel coordinates differ by over 100 km; local name, city and coordinates retained"
        canonical_city = reference["name"] if effective_kind == "city" else (
            reference.get("city_name") or (reference.get("city") or {}).get("name") or "")
        canonical = {"name": reference["name"], "city": canonical_city,
                     "country": local_country if country_exception else reference["iata_country_code"]}
        for field, value in canonical.items():
            if place.get(field) != value:
                row.setdefault("reference_differences", {})[field] = {"before": place.get(field), "after": value}
            if distant and field in {"name", "city"}:
                continue
            if place.get(field) != value:
                row["changes"][field] = {"before": place.get(field), "after": value}
            new[field] = value
        new["duffel_id"] = reference["id"]
        new.pop("validation_warnings", None)
        new.pop("duffel_country", None)
        warnings = []
        if country_exception:
            new["duffel_country"] = reference["iata_country_code"]
            warnings.append("provider_country_differs")
        if distant:
            warnings.append("provider_coordinates_differ")
        if warnings:
            new["validation_warnings"] = warnings
        if effective_kind == "airport":
            city = reference.get("city") or {}
            new["iata_city_code"] = reference.get("iata_city_code") or city.get("iata_code")
            # Keep established local coordinates. Duffel's reference geography is
            # demonstrably inconsistent for some airports and affects geolocation.
            if not old_point and point:
                new["latitude"], new["longitude"] = point
                row["coordinate_source"] = "duffel_fallback"
        else:
            new["type"] = "city"
            new["airport_codes"] = sorted({airport["iata_code"] for airport in reference["airports"]
                if isinstance(airport, dict) and re.fullmatch(r"[A-Z]{3}", str(airport.get("iata_code", "")))})
            if converted_to_city:
                row["changes"]["type"] = {"before": place["type"], "after": "city"}
                new.pop("latitude", None)
                new.pop("longitude", None)
                new.pop("iata_city_code", None)
        aliases = set() if converted_to_city else set(place.get("aliases", []))
        if not distant and not converted_to_city:
            aliases.update(value for value in [place.get("name"), place.get("city")] if value)
        new["aliases"] = sorted(value for value in aliases if normalized(value) not in
                                {normalized(new["name"]), normalized(new["city"])})
        if not new["aliases"]:
            new.pop("aliases")
        new["label"] = " - ".join(filter(None, [new["name"], new["city"] if new["city"] != new["name"] else "", new["country"]]))
        proposed.append(new)
    return results, proposed


def write_reports(directory: Path, local: list[dict], snapshot: dict, results: list[dict], proposed: list[dict]) -> dict:
    summary = {"validation_status": "complete_reference_comparison", "retrieved_at": snapshot["retrieved_at"],
               "provider": "Duffel", "api_version": "v2", "sources": snapshot["sources"],
               "reference_counts": {"airports": len(snapshot["airports"]), "cities": len(snapshot["cities"]),
                                    "additional_airports_in_city_membership": len(merged_airports(snapshot["airports"], snapshot["cities"])) - len(snapshot["airports"])},
               "pages": snapshot.get("pages"),
               "token_mode": snapshot.get("token_mode", "unknown"),
               "retained_count": len(proposed),
               "excluded_count": len(local) - len(proposed),
               "coordinate_policy": "Keep local coordinates; use Duffel only when local coordinates are missing",
               "reference_sha256": digest({"airports": snapshot["airports"], "cities": snapshot["cities"]}),
               "input_sha256": digest(local), "proposed_sha256": digest(proposed), "local_count": len(local),
               "status_counts": dict(sorted(Counter(row["status"] for row in results).items())),
               "metadata_changes": sum(bool(row["changes"]) for row in results),
               "coordinates_over_100km": [row["code"] for row in results if row.get("coordinate_distance_km", 0) > 100],
               "scope": "Reference identity validation only; not a guarantee of flight availability, scheduled service or bookability."}
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in [("summary.json", summary), ("comparison.json", results), ("proposed-airports.json", proposed)]:
        (directory / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (directory / "comparison.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, lineterminator="\n", fieldnames=["code", "type", "local_name", "local_country", "status", "duffel_id", "duffel_name", "coordinate_distance_km", "metadata_warning", "duffel_country", "changes", "reference_differences"], extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow({**row, "changes": json.dumps(row["changes"], ensure_ascii=False), "reference_differences": json.dumps(row.get("reference_differences", {}), ensure_ascii=False)})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "airports.json")
    parser.add_argument("--output", type=Path, required=True, help="Directory for evidence and proposed corrections")
    parser.add_argument("--write-catalog", action="store_true", help="Replace catalog with confirmed records after reviewing the report")
    args = parser.parse_args()
    token = os.environ.get("DUFFEL_ACCESS_TOKEN", "").strip().strip('\"').strip("'")
    if not token:
        print("DUFFEL_ACCESS_TOKEN is required. Set it locally; never paste it into a report or commit it.", file=sys.stderr)
        return 2
    try:
        local = json.loads(args.catalog.read_text(encoding="utf-8"))
        if not isinstance(local, list) or any(not isinstance(place, dict) for place in local):
            raise CatalogError("Local catalog must be an array of objects")
        with requests.Session() as session:
            session.headers.update({"Authorization": f"Bearer {token}", "Duffel-Version": "v2", "Accept": "application/json"})
            airports, airport_pages = fetch_list(session, "airport")
            cities, city_pages = fetch_list(session, "city")
        snapshot = {"retrieved_at": datetime.now(timezone.utc).isoformat(), "sources": [API_ROOT + path for path in SOURCES.values()],
                    "pages": {"airports": airport_pages, "cities": city_pages},
                    "token_mode": "test" if token.startswith("duffel_test_") else "live" if token.startswith("duffel_live_") else "unknown",
                    "airports": airports, "cities": cities}
        results, proposed = compare_catalog(local, airports, cities)
        if not proposed:
            raise CatalogError("No records confirmed; catalog will not be replaced")
        summary = write_reports(args.output, local, snapshot, results, proposed)
        (args.output / "duffel-reference.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        if args.write_catalog:
            temporary = args.catalog.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(proposed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(args.catalog)
            print(f"Wrote {len(proposed):,} confirmed records to {args.catalog}")
        return 0
    except (CatalogError, OSError, ValueError) as error:
        print(f"Validation stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
