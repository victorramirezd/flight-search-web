# Istanbul airport selection and empty-response parsing — 1 November 2026

The reported MIL→ISL round trip, 13–16 December 2026, reproduced locally.
Google returned HTTP 200 with both flight-result sections null. Its airline
metadata section was omitted, although alliance metadata remained. The pinned
fast-flights parser indexed `payload[7][1][1]` and raised IndexError. This is a
different failure from Google's explicit generic-error payloads.

The catalogue incorrectly offered Atatürk (ISL) as the first Istanbul airport
and lacked a metropolitan choice. DHMI's 2019 transfer report states that
Atatürk closed to scheduled/unscheduled commercial passenger flights on 6 April
2019. Istanbul Airport uses IST; Sabiha Gökçen uses SAW.

Sources:
- https://www.dhmi.gov.tr/Lists/AnnualReports/Attachments/13/2019.pdf
- https://www.dhmi.gov.tr/Sayfalar/Havalimani/Istanbul/GenelBilgiler.aspx
- https://www.sgairport.com/en/general-information-about-the-airport/

## Fix deployed in 9ea5e4a

Google-specific catalogue overrides add Istanbul (All airports), covering IST and
SAW. Its internal selector IST_ALL maps to observed Google entity `/m/09949m`,
with city type 3. The existing IST and SAW selectors remain individual airports.
ISL is hidden from suggestions and rejected with a helpful message before search.
The reviewed base catalogue and its audit remain unchanged.

The adapter recognizes the narrowly defined empty-response shape before the
upstream parser. It returns no matching priced results while retaining unknown
availability. Malformed responses and Google's generic errors retain error
handling. A minimized captured-response fixture protects against recurrence.

## Verification and remaining limitation

89 Python tests, three JavaScript suites and inline browser-script syntax passed.
The deployed autocomplete visibly displayed Istanbul (All airports) first.
Production rejected ISL with HTTP 400 and the correct alternatives.

Bounded local checks for 13–16 December returned 11 offers for IST and 6 for SAW.
The combined city search returned 10 offers, with actual endpoints in both IST
and SAW. These observations do not establish current or future fare availability.

Production tests of the combined city choice on 13→16, 16→19 and 16→20 December
still received Google's generic HTTP-200 error, classified `upstream_error` rather
than `parser_error`. Execution remained in US region iad1, with revision
9ea5e4a07765bebdf47f5ddf784d82b70898a29d. The catalogue/parser fixes do not establish
a solution to that separate production-provider reliability problem. No regional
migration, browser fallback, pacing change or extra automatic retries were added.

Anonymous captured responses and bounded production events remain locally in
ignored `work/diagnostics/istanbul-2026-11-01/`. Raw HTML was not committed.
