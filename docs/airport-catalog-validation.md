# Full airport catalog validation against Duffel

Validated on 17 October 2026 using the configured live Duffel connection.
Only reference-data GET requests were made: no offer searches or bookings.

## Coverage and decisions

Both endpoints were paginated to a null `meta.after` cursor: 46 airport pages
(9,045 records) and 2 city pages (255 records). City membership supplied another
7 airport identities not already represented by a top-level code/country pair.
Top-level records take precedence over duplicate city-membership aliases.

All 8,820 existing catalog entries were compared. The resulting catalog contains
8,543 entries; all have a corresponding Duffel airport or city ID.

| Outcome | Entries | Action |
|---|---:|---|
| Code, location type and country agree | 8,524 | Retain; reconcile metadata |
| Same code and airport within 5 km, country differs | 17 | Retain local geography/country; record provider discrepancy |
| Duffel defines the code as a city, not the old airport | 2 | Relabel as a city; discard old airport geometry and aliases |
| Country/code conflict without nearby identity evidence | 19 | Exclude from selectable catalog; retain audit evidence |
| Code absent from both complete reference lists, including city membership | 258 | Exclude from selectable catalog; retain audit evidence |

There are 3,708 entries with applied name, city, country-format or type
corrections. Airport-size rankings remain from the original catalog because Duffel
does not supply those classifications. This validates the existing code set; it
does not import every additional airport or city supported by Duffel.

## Important cases

- **Palermo / PMO** is confirmed as `arp_pmo_it`. Its canonical Duffel name is
  Falcone-Borsellino Airport; the original spelling is preserved as a search alias.
- **IZM** is a Duffel city search for Izmir, not the old Selçuk Efes Airport label.
- **PBI** is a Duffel city search for West Palm Beach. Its current city-membership
  airport codes are taken directly from Duffel; the old individual-airport label
  is not transferred to the city selection.
- **MKC** is still an airport in Duffel's Kansas City membership even though it is
  absent from the top-level airport list. It remains selectable as an airport.
- **HGI** maps to an airport in South Sudan in Duffel, while the old catalog
  labelled it as Itanagar in India. This is excluded rather than silently routing
  a user to the wrong location. All excluded entries are listed in the CSV.

## Metadata conflicts

Duffel's reference metadata is not uniformly reliable. There are 101 retained
records whose provider coordinates differ by more than 100 km. Some differences
are thousands of kilometres, such as Robinhood Airport (ROH). The original local
coordinates, name and city are kept for these records. The raw provider fields
and measured distances are recorded in the audit.

For all retained airports, existing local coordinates are preserved; valid Duffel
coordinates are used only if local coordinates are absent. Neither source is
asserted to be independently geospatially certified. This avoids propagating
known provider inconsistencies into nearest-airport detection.

The 17 close geographic matches with differing country fields are:

| Code | Local country retained | Duffel country | Distance (km) |
|---|---|---|---:|
| BQI | NA | ML | 0.122 |
| GBA | GB | VU | 0.182 |
| WDN | US | CN | 0.046 |
| EUN | EH | MA | 0.991 |
| SWV | RU | PK | 0.079 |
| SFG | MF | GP | 0.021 |
| JAR | IR | US | 0.094 |
| KBH | TZ | PK | 0.279 |
| BON | BQ | AN | 0.474 |
| RYL | ZM | ZW | 0.762 |
| MAK | SS | SD | 0.037 |
| SCR | SE | US | 0.98 |
| CYD | BZ | GT | 0.052 |
| EUX | BQ | AN | 0.102 |
| SBT | RU | US | 0.056 |
| WUU | SS | SD | 0.065 |
| SAB | BQ | NL | 0.054 |

These are explicit metadata exceptions, not silent country replacements. Warning
fields remain in the catalog and per-entry CSV for follow-up with Duffel.

## Evidence and refresh

- `data/airport-validation.csv`: one outcome per original entry, applied changes,
  provider identity/name/country and coordinate discrepancies.
- `data/airport-validation.json`: completion status, page counts, retrieval time,
  source URLs, status totals and SHA-256 digests of input, reference and output.
- `scripts/validate_duffel_catalog.py`: repeatable downloader and comparator.
- `tests/test_duffel_catalog.py`: pagination, failure safety, identity conflicts,
  membership-only airports and metadata reconciliation.
- `tests/test_catalog_integrity.py`: the shipped catalog matches the evidence;
  excluded codes cannot silently remain selectable.

The complete raw snapshot and comparison JSON are saved locally in ignored
`work/duffel-catalog/`. No credentials are written to reports or committed.
The SHA-256 values use JSON with sorted keys and compact separators, as implemented
by `digest()` in the validator. Re-running validation against this corrected
catalog creates a new baseline rather than overwriting the historical audit until
its outputs are explicitly copied into `data/`.

Reference identity does **not** guarantee scheduled service, available offers or
bookability. Codes may change or be reassigned, so repeat the audit periodically.
The older OurAirports coordinate script refuses to overwrite a catalog with
Duffel validation IDs; refresh through the validator instead.

Duffel documentation:
- [List airports](https://duffel.com/docs/api/airports/get-airports)
- [List cities](https://duffel.com/docs/api/v2/cities)
- [Pagination](https://duffel.com/docs/api/overview/pagination)
