from __future__ import annotations

import json
import os
import re
import sys
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from flight_search import (ApiError, build_search_config, iter_search_rows, search_queries,
                           validate_retry_queries)
from google_flights import (GoogleFlightsClient, REQUEST_INTERVAL_SECONDS,
                            REQUEST_COUNTERS, validate_request_interval_seconds, validate_session_mode)


SEARCH_PASSWORD = "victor"


def deployment_metadata():
    """Expose only deployment identifiers, never arbitrary environment values."""
    def safe(value, limit):
        return value[:limit] if value and re.fullmatch(r"[A-Za-z0-9_-]+", value) else None
    return {"region": safe(os.environ.get("VERCEL_REGION"), 32) or "local",
            "revision": safe(os.environ.get("VERCEL_GIT_COMMIT_SHA"), 40)}


def diagnostic_log(event, request_id, **details):
    print(json.dumps({"event": event, "request_id": request_id, **details}), file=sys.stderr, flush=True)


def json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def stream_event(handler: BaseHTTPRequestHandler, payload: Dict[str, Any]) -> None:
    line = json.dumps(payload).encode("utf-8") + b"\n"
    handler.wfile.write(line)
    handler.wfile.flush()


def parse_int(data: Dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if value == "" or value is None:
        return default
    return int(value)


def parse_bool(data: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length)
            data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, json.JSONDecodeError):
            json_response(self, 400, {"error": "Invalid JSON request body."})
            return

        if str(data.get("password", "")).lower() != SEARCH_PASSWORD:
            json_response(self, 401, {"error": "Incorrect password."})
            return

        try:
            session_mode = validate_session_mode(data.get('session_mode', 'shared'))
            query_encoding = data.get("query_encoding", "browser")
            if not isinstance(query_encoding, str) or query_encoding not in {"browser", "legacy"}:
                raise ValueError("query_encoding must be 'browser' or 'legacy'.")
            request_interval_seconds = validate_request_interval_seconds(
                data.get("request_interval_seconds", REQUEST_INTERVAL_SECONDS))
            config = build_search_config(
                origin=str(data.get("origin", "")),
                destination=str(data.get("destination", "")),
                target_date=str(data.get("target_date", "")),
                date_flex_days=parse_int(data, "date_flex_days", 4),
                one_way=parse_bool(data, "one_way", False),
                min_duration=parse_int(data, "min_duration", 13),
                max_duration=parse_int(data, "max_duration", 17),
                adults=parse_int(data, "adults", 1),
                max_results_per_query=parse_int(data, "max_results_per_query", 20),
                min_connections_per_slice=parse_int(data, "min_connections_per_slice", 0),
                max_connections_per_slice=parse_int(data, "max_connections_per_slice", 2),
                exclude_self_transfers=parse_bool(data, "exclude_self_transfers", True),
            )
            plan = (validate_retry_queries(data["retry_queries"], config)
                    if "retry_queries" in data else search_queries(config))
        except (TypeError, ValueError) as err:
            json_response(self, 400, {"error": str(err)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        request_id = str(uuid.uuid4())
        metadata = {"request_id": request_id, **deployment_metadata(),
                    "request_interval_seconds": request_interval_seconds,
                    "query_encoding": query_encoding, "session_mode": session_mode}
        rows = []
        ledger = {q.payload()["query_id"]: {**q.payload(), "status": "pending", "message": ""}
                  for q in plan}

        def report_progress(event):
            if event["status"] != "running":
                event["diagnostics"] = {**event.get("diagnostics", {}),
                                        "request_interval_seconds": request_interval_seconds,
                                        "session_mode": session_mode,
                                        "query_encoding": event.get("diagnostics", {}).get("query_encoding", query_encoding)}
            ledger[event["query_id"]].update(event)
            if event["status"] != "running":
                diagnostic_log("flight_query", request_id, query_id=event["query_id"],
                               status=event["status"], diagnostics=event.get("diagnostics", {}),
                               query_encoding=query_encoding, session_mode=session_mode)
            stream_event(self, {"type": "progress", **event})

        def terminal_details():
            counts = {key: sum(q.get('diagnostics', {}).get(key, 0) for q in ledger.values())
                      for key in REQUEST_COUNTERS}
            return {**metadata, "request_counts": counts,
                    "failed_queries": [q for q in ledger.values() if q["status"] == "error"],
                    "pending_queries": [q for q in ledger.values() if q["status"] == "pending"],
                    "completed": sum(q["status"] not in {"pending", "running"} for q in ledger.values()),
                    "total": len(plan)}

        try:
            diagnostic_log("flight_search_start", request_id, **deployment_metadata(),
                           total=len(plan), is_retry="retry_queries" in data,
                           request_interval_seconds=request_interval_seconds, query_encoding=query_encoding, session_mode=session_mode)
            stream_event(
                self,
                {
                    "type": "start",
                    "one_way": config.one_way,
                    "provider": "google_flights",
                    "currency": "EUR",
                    "queries": [q.payload() for q in plan],
                    "total": len(plan),
                    "is_retry": "retry_queries" in data,
                    **metadata,
                },
            )
            client = GoogleFlightsClient(
                min_connections=config.min_connections_per_slice,
                max_connections=config.max_connections_per_slice,
                exclude_self_transfers=config.exclude_self_transfers,
                request_interval_seconds=request_interval_seconds,
                query_encoding=query_encoding,
                session_mode=session_mode,
            )
            for row in iter_search_rows(client, config, queries=plan, on_progress=report_progress):
                rows.append(row)
                stream_event(self, {"type": "row", "row": row, "searched": len(rows)})
        except ApiError as err:
            terminal = terminal_details()
            diagnostic_log("flight_search_error", request_id, completed=terminal["completed"],
                           total=len(plan), diagnostics=err.diagnostics,
                           request_interval_seconds=request_interval_seconds, query_encoding=query_encoding, session_mode=session_mode)
            stream_event(self, {"type": "error", "error": str(err), "status": 502,
                                "searched": len(rows), "incomplete": True,
                                "diagnostics": err.diagnostics, **terminal})
            return
        except (BrokenPipeError, ConnectionResetError):
            diagnostic_log("flight_search_disconnected", request_id,
                           completed=terminal_details()["completed"], total=len(plan),
                           request_interval_seconds=request_interval_seconds, query_encoding=query_encoding, session_mode=session_mode)
            return

        terminal = terminal_details()
        diagnostic_log("flight_search_done", request_id, completed=terminal["completed"], total=len(plan),
                       failed=len(terminal["failed_queries"]), request_interval_seconds=request_interval_seconds,
                       query_encoding=query_encoding, session_mode=session_mode)
        stream_event(self, {"type": "done", "rows": rows, "searched": len(rows), **terminal})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()
