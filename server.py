#!/usr/bin/env python3
"""SAM Radar — a dependency-free opportunity intelligence dashboard.

Run locally with ``python3 server.py``.  The same process is safe to run on a
small VPS or a managed Python service; deployment files live beside this file.
"""
from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("RADAR_DB_PATH", ROOT / "radar.db"))
SETTINGS_PATH = Path(os.getenv("RADAR_SETTINGS_PATH", ROOT / "settings.json"))
INDEX_PATH = ROOT / "index.html"
UTC = dt.timezone.utc

DEFAULT_SETTINGS = {
    "app": {
        "port": 8765,
        "public_url": "",
        "access_password": "",
        "open_browser": True,
        "tls_ca_file": "",
    },
    "sam": {
        "api_key": "",
        "base_url": "https://api.sam.gov/opportunities/v2/search",
        "naics_codes": ["541511", "541512", "541513", "541519", "518210"],
        "notice_types": ["p", "k", "o", "r", "s"],
    },
    "sync": {
        "incremental_days": 3,
        "first_backfill_days": 90,
        "backfill_chunk_days": 7,
        "page_size": 100,
        "request_gap_seconds": 0.70,
        "retry_attempts": 5,
        "daily_sync_time": "07:30",
        "auto_sync": True,
    },
    "score": {
        "keywords": ["digital", "software", "cloud", "data", "cyber", "agile", "devops", "human centered", "user experience"],
        "set_asides": ["SBA", "SBP"],
        "min_alert_score": 4,
        "weights": {
            "naics_match": 3,
            "keyword_first": 2,
            "keyword_extra": 1,
            "priority_agency": 2,
            "set_aside_match": 1,
            "sources_sought": 1,
            "active_solicitation": 1,
        },
    },
    "agencies": [
        {"code": "VA", "label": "Veterans Affairs", "match": "VETERANS AFFAIRS", "enabled": True},
        {"code": "GSA", "label": "General Services Administration", "match": "GENERAL SERVICES ADMINISTRATION", "enabled": True},
        {"code": "HHS", "label": "Health and Human Services", "match": "HEALTH AND HUMAN SERVICES", "enabled": True},
        {"code": "SSA", "label": "Social Security Administration", "match": "SOCIAL SECURITY ADMINISTRATION", "enabled": True},
        {"code": "TREAS", "label": "Department of the Treasury", "match": "TREASURY", "enabled": True},
        {"code": "STATE", "label": "Department of State", "match": "DEPARTMENT OF STATE", "enabled": True},
        {"code": "DOJ", "label": "Department of Justice", "match": "DEPARTMENT OF JUSTICE", "enabled": True},
        {"code": "DOL", "label": "Department of Labor", "match": "DEPARTMENT OF LABOR", "enabled": True},
        {"code": "DHS", "label": "Department of Homeland Security", "match": "HOMELAND SECURITY", "enabled": True},
        {"code": "DOC", "label": "Department of Commerce", "match": "DEPARTMENT OF COMMERCE", "enabled": True},
    ],
    "email": {
        "enabled": False,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "smtp_username": "",
        "smtp_password": "",
        "from_address": "",
        "to_addresses": [],
        "send_time": "08:00",
        "auto_send": False,
        "last_auto_result": "Not run yet.",
        "last_auto_at": "",
    },
}

SETTINGS_LOCK = threading.RLock()
JOB_LOCK = threading.RLock()
JOB = {"running": False, "kind": "", "message": "Ready", "done": 0, "total": 0,
       "started_at": "", "finished_at": "", "result": "", "run_id": None}


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat()


def date_iso(value) -> str:
    """Return a YYYY-MM-DD date from SAM's varied date formats."""
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(text[:25], fmt).date().isoformat()
        except ValueError:
            pass
    return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else ""


def deep_merge(base: dict, incoming: dict) -> dict:
    result = dict(base)
    for key, value in (incoming or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def env_override(settings: dict) -> dict:
    """Keep secrets out of a deployment's disk when environment variables exist."""
    pairs = {
        "SAM_API_KEY": ("sam", "api_key"),
        "RADAR_ACCESS_PASSWORD": ("app", "access_password"),
        "RADAR_PUBLIC_URL": ("app", "public_url"),
        "RADAR_SMTP_PASSWORD": ("email", "smtp_password"),
        "RADAR_SMTP_USERNAME": ("email", "smtp_username"),
        "RADAR_FROM_ADDRESS": ("email", "from_address"),
        "RADAR_TO_ADDRESSES": ("email", "to_addresses"),
    }
    for env_name, (section, key) in pairs.items():
        value = os.getenv(env_name)
        if value is not None and value != "":
            settings[section][key] = ([x.strip() for x in value.split(",") if x.strip()]
                                      if key == "to_addresses" else value)
    return settings


def load_settings() -> dict:
    with SETTINGS_LOCK:
        stored = {}
        if SETTINGS_PATH.exists():
            try:
                stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
        return env_override(deep_merge(DEFAULT_SETTINGS, stored))


def save_settings(settings: dict) -> None:
    """Atomically write settings without copying deployment secrets onto disk."""
    with SETTINGS_LOCK:
        stored = json.loads(json.dumps(settings))
        # A deployment can inject these from its secret manager.  Do not turn an
        # environment-only secret into a value in the mounted settings file just
        # because another setting was changed in the dashboard.
        for env_name, section, key in (
            ("SAM_API_KEY", "sam", "api_key"),
            ("RADAR_ACCESS_PASSWORD", "app", "access_password"),
            ("RADAR_SMTP_PASSWORD", "email", "smtp_password"),
        ):
            if os.getenv(env_name):
                stored[section][key] = ""
        temporary = SETTINGS_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(stored, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(SETTINGS_PATH)


def public_settings(settings: dict) -> dict:
    clean = json.loads(json.dumps(settings))
    clean["sam"]["api_key"] = "configured" if settings["sam"].get("api_key") else ""
    clean["app"]["access_password"] = "configured" if settings["app"].get("access_password") else ""
    for key in ("smtp_password",):
        clean["email"][key] = "configured" if settings["email"].get(key) else ""
    return clean


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS opportunities (
            notice_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            agency_code TEXT,
            agency_name TEXT,
            parent_path TEXT,
            notice_type TEXT,
            notice_type_code TEXT,
            naics_code TEXT,
            set_aside TEXT,
            posted_date TEXT,
            response_deadline TEXT,
            description TEXT,
            point_of_contact TEXT,
            sam_url TEXT,
            fit_score INTEGER NOT NULL DEFAULT 0,
            fit_reasons TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            changed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_opp_fit ON opportunities(fit_score DESC);
        CREATE INDEX IF NOT EXISTS idx_opp_due ON opportunities(response_deadline);
        CREATE INDEX IF NOT EXISTS idx_opp_agency ON opportunities(agency_code);
        CREATE TABLE IF NOT EXISTS notifications (
            notice_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (notice_id, reason)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            unchanged INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """)


def state_get(conn: sqlite3.Connection, key: str, default=""):
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def state_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("""INSERT INTO sync_state(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                 (key, str(value), now_iso()))


def safe_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(safe_text(item) for item in value if item)
    if isinstance(value, dict):
        return " ".join(safe_text(item) for item in value.values() if item)
    return str(value).strip()


NOTICE_TYPE_LABELS = {
    "p": "Pre-solicitation", "k": "Combined Synopsis/Solicitation", "o": "Solicitation",
    "r": "Sources Sought", "s": "Special Notice", "a": "Award Notice",
}


def normalize(item: dict) -> dict:
    """Map SAM responses to one stable, display-friendly record."""
    office = item.get("officeAddress") or {}
    contact = item.get("pointOfContact") or item.get("pointOfContacts") or []
    if isinstance(contact, list):
        contact = "; ".join(" ".join(x for x in [safe_text(c.get("fullName")), safe_text(c.get("email")), safe_text(c.get("phone"))] if x)
                            for c in contact if isinstance(c, dict))
    notice_id = safe_text(item.get("noticeId") or item.get("solicitationNumber") or item.get("id"))
    posted = date_iso(item.get("postedDate") or item.get("publishDate"))
    deadline = date_iso(item.get("responseDeadLine") or item.get("responseDeadline") or item.get("archiveDate"))
    notice_code = safe_text(item.get("type") or item.get("noticeType") or "").lower()
    notice_type = safe_text(item.get("typeOfNotice") or item.get("noticeTypeLabel") or NOTICE_TYPE_LABELS.get(notice_code) or notice_code)
    parent = safe_text(item.get("fullParentPathName") or item.get("parentPathName"))
    agency = safe_text(item.get("department") or item.get("organizationType") or item.get("subTier"))
    title = safe_text(item.get("title") or item.get("solicitationNumber") or "Untitled opportunity")
    return {
        "notice_id": notice_id or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()[:24],
        "title": title,
        "agency_code": "",
        "agency_name": agency or parent or safe_text(office.get("city")),
        "parent_path": parent,
        "notice_type": notice_type.title() if notice_type else "Other",
        "notice_type_code": notice_code,
        "naics_code": safe_text(item.get("naicsCode") or item.get("naics") or ""),
        "set_aside": safe_text(item.get("typeOfSetAside") or item.get("setAside") or item.get("typeOfSetAsideDescription")),
        "posted_date": posted,
        "response_deadline": deadline,
        "description": safe_text(item.get("description") or item.get("additionalInfoLink") or ""),
        "point_of_contact": safe_text(contact),
        "sam_url": safe_text(item.get("uiLink") or item.get("link") or ""),
        "raw_json": json.dumps(item, sort_keys=True, separators=(",", ":")),
    }


def score_row(row: dict | sqlite3.Row, settings: dict) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    conf = settings["score"]
    weights = conf["weights"]
    naics = str(row["naics_code"] or "")
    if naics in set(map(str, settings["sam"].get("naics_codes", []))):
        score += int(weights["naics_match"])
        reasons.append(f"+{weights['naics_match']} configured NAICS {naics}")

    agency_text = " ".join([str(row["agency_name"] or ""), str(row["parent_path"] or "")]).upper()
    for agency in settings.get("agencies", []):
        if agency.get("enabled") and agency.get("match", "").upper() in agency_text:
            row["agency_code"] = agency["code"] if isinstance(row, dict) else row["agency_code"]
            score += int(weights["priority_agency"])
            reasons.append(f"+{weights['priority_agency']} priority agency: {agency['label']}")
            break

    content = " ".join(str(row.get(k, "") if isinstance(row, dict) else row[k] or "")
                       for k in ("title", "description")).lower()
    hits = []
    for keyword in conf.get("keywords", []):
        keyword = keyword.strip().lower()
        if keyword and re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", content):
            hits.append(keyword)
    if hits:
        score += int(weights["keyword_first"])
        reasons.append(f"+{weights['keyword_first']} keyword: {hits[0]}")
        for keyword in hits[1:4]:
            score += int(weights["keyword_extra"])
            reasons.append(f"+{weights['keyword_extra']} additional keyword: {keyword}")

    set_aside = str(row["set_aside"] or "").upper()
    if any(code.upper() in set_aside for code in conf.get("set_asides", [])):
        score += int(weights["set_aside_match"])
        reasons.append(f"+{weights['set_aside_match']} eligible set-aside: {row['set_aside']}")
    notice_code = str(row["notice_type_code"] or "").lower()
    if notice_code == "r" or "sources sought" in str(row["notice_type"] or "").lower():
        score += int(weights["sources_sought"])
        reasons.append(f"+{weights['sources_sought']} early shaping opportunity")
    elif notice_code in ("o", "k") or "solicitation" in str(row["notice_type"] or "").lower():
        score += int(weights["active_solicitation"])
        reasons.append(f"+{weights['active_solicitation']} active solicitation")
    return score, reasons


def resolved_agency(row: dict, settings: dict) -> str:
    text = (row.get("agency_name", "") + " " + row.get("parent_path", "")).upper()
    for agency in settings.get("agencies", []):
        if agency.get("match", "").upper() in text:
            return agency["code"]
    return row.get("agency_code", "")


def ingest(conn: sqlite3.Connection, items: list[dict], settings: dict) -> dict:
    outcome = {"inserted": 0, "updated": 0, "unchanged": 0}
    stamp = now_iso()
    for item in items:
        record = normalize(item)
        record["agency_code"] = resolved_agency(record, settings)
        score, reasons = score_row(record, settings)
        existing = conn.execute("SELECT raw_json FROM opportunities WHERE notice_id = ?", (record["notice_id"],)).fetchone()
        if existing and existing["raw_json"] == record["raw_json"]:
            conn.execute("UPDATE opportunities SET last_seen_at=? WHERE notice_id=?", (stamp, record["notice_id"]))
            outcome["unchanged"] += 1
            continue
        values = (record["notice_id"], record["title"], record["agency_code"], record["agency_name"], record["parent_path"],
                  record["notice_type"], record["notice_type_code"], record["naics_code"], record["set_aside"],
                  record["posted_date"], record["response_deadline"], record["description"], record["point_of_contact"],
                  record["sam_url"], score, json.dumps(reasons), record["raw_json"], stamp, stamp, stamp)
        conn.execute("""INSERT INTO opportunities(notice_id,title,agency_code,agency_name,parent_path,notice_type,notice_type_code,
                    naics_code,set_aside,posted_date,response_deadline,description,point_of_contact,sam_url,fit_score,fit_reasons,
                    raw_json,first_seen_at,last_seen_at,changed_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(notice_id) DO UPDATE SET
                    title=excluded.title, agency_code=excluded.agency_code, agency_name=excluded.agency_name,
                    parent_path=excluded.parent_path, notice_type=excluded.notice_type, notice_type_code=excluded.notice_type_code,
                    naics_code=excluded.naics_code, set_aside=excluded.set_aside, posted_date=excluded.posted_date,
                    response_deadline=excluded.response_deadline, description=excluded.description, point_of_contact=excluded.point_of_contact,
                    sam_url=excluded.sam_url, fit_score=excluded.fit_score, fit_reasons=excluded.fit_reasons,
                    raw_json=excluded.raw_json, last_seen_at=excluded.last_seen_at, changed_at=excluded.changed_at""", values)
        outcome["updated" if existing else "inserted"] += 1
    return outcome


class RateLimiter:
    """One pace clock across every SAM request, even if future jobs overlap."""
    def __init__(self):
        self.lock = threading.Lock()
        self.next_allowed = 0.0

    def wait(self, seconds: float) -> None:
        with self.lock:
            current = time.monotonic()
            wait_for = max(0.0, self.next_allowed - current)
            self.next_allowed = max(current, self.next_allowed) + max(0.1, seconds)
        if wait_for:
            time.sleep(wait_for)


SAM_LIMITER = RateLimiter()


def ssl_context(settings: dict):
    context = ssl.create_default_context()
    ca_path = settings["app"].get("tls_ca_file", "")
    if ca_path:
        context.load_verify_locations(cafile=ca_path)
    return context


def sam_get(params: dict, settings: dict, request_counter: list[int]) -> dict:
    """A rate-limit aware GET.  429's are a cue to slow down, never an error loop."""
    retry_attempts = max(1, int(settings["sync"].get("retry_attempts", 5)))
    for attempt in range(retry_attempts):
        SAM_LIMITER.wait(float(settings["sync"].get("request_gap_seconds", 0.7)))
        request_counter[0] += 1
        url = settings["sam"].get("base_url", DEFAULT_SETTINGS["sam"]["base_url"]) + "?" + urllib.parse.urlencode(params)
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "SAM-Radar/2.0"})
            with urllib.request.urlopen(request, timeout=45, context=ssl_context(settings)) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read(500).decode("utf-8", "replace")
            if error.code not in (429, 500, 502, 503, 504) or attempt == retry_attempts - 1:
                raise RuntimeError(f"SAM.gov HTTP {error.code}: {body}") from error
            retry_after = error.headers.get("Retry-After", "")
            try:
                delay = min(120, max(1, float(retry_after)))
            except ValueError:
                delay = min(60, 2 ** attempt + 1)
            with SAM_LIMITER.lock:
                SAM_LIMITER.next_allowed = max(SAM_LIMITER.next_allowed, time.monotonic() + delay)
            job_message(f"SAM.gov asked us to slow down; resuming in {int(delay)}s")
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == retry_attempts - 1:
                raise RuntimeError(f"SAM.gov request failed: {error}") from error
            time.sleep(min(30, 2 ** attempt + 1))
    raise RuntimeError("SAM.gov request retry limit reached")


def fetch_window(settings: dict, from_date: dt.date, to_date: dt.date, naics_code: str, request_counter: list[int]) -> list[dict]:
    """Fetch a single NAICS/time slice serially, paging just until SAM is exhausted."""
    found = []
    offset = 0
    page_size = min(1000, max(1, int(settings["sync"].get("page_size", 100))))
    while True:
        payload = sam_get({
            "api_key": settings["sam"]["api_key"],
            "postedFrom": from_date.strftime("%m/%d/%Y"),
            "postedTo": to_date.strftime("%m/%d/%Y"),
            "ncode": naics_code,
            "ptype": ",".join(settings["sam"].get("notice_types", [])),
            "limit": page_size,
            "offset": offset,
        }, settings, request_counter)
        page = payload.get("opportunitiesData") or []
        found.extend(page)
        total = int(payload.get("totalRecords") or 0)
        offset += len(page)
        if not page or offset >= total:
            return found


def job_update(**fields) -> None:
    with JOB_LOCK:
        JOB.update(fields)


def job_message(message: str) -> None:
    job_update(message=message)


def date_windows(start: dt.date, end: dt.date, chunk_days: int):
    current = start
    chunk_days = max(1, chunk_days)
    while current <= end:
        finish = min(end, current + dt.timedelta(days=chunk_days - 1))
        yield current, finish
        current = finish + dt.timedelta(days=1)


def start_sync(kind: str = "incremental", days: int | None = None) -> tuple[bool, str]:
    with JOB_LOCK:
        if JOB["running"]:
            return False, "A sync is already running."
        JOB.update({"running": True, "kind": kind, "message": "Preparing sync…", "done": 0, "total": 0,
                    "started_at": now_iso(), "finished_at": "", "result": "", "run_id": None})
    threading.Thread(target=sync_worker, args=(kind, days), daemon=True, name="sam-radar-sync").start()
    return True, "Sync started."


def sync_worker(kind: str, requested_days: int | None) -> None:
    settings = load_settings()
    api_key = settings["sam"].get("api_key", "")
    if not api_key:
        job_update(running=False, finished_at=now_iso(), message="Add a SAM.gov API key in Settings before syncing.", result="No API key")
        return
    end = dt.date.today()
    if kind == "backfill":
        days = requested_days or int(settings["sync"].get("first_backfill_days", 90))
        start = end - dt.timedelta(days=max(1, days) - 1)
        chunk = int(settings["sync"].get("backfill_chunk_days", 7))
    else:
        days = requested_days or int(settings["sync"].get("incremental_days", 3))
        # An overlap catches late corrections from SAM without revisiting the full history.
        start = end - dt.timedelta(days=max(1, days) - 1)
        chunk = max(1, days)
    windows = list(date_windows(start, end, chunk))
    naics_codes = [str(code) for code in settings["sam"].get("naics_codes", []) if str(code).strip()]
    total = len(windows) * len(naics_codes)
    requests = [0]
    results = {"inserted": 0, "updated": 0, "unchanged": 0}
    with connect() as conn:
        cur = conn.execute("INSERT INTO sync_runs(started_at,kind,status) VALUES(?,?,?)", (now_iso(), kind, "running"))
        run_id = cur.lastrowid
        conn.commit()
    job_update(run_id=run_id, total=total)
    try:
        with connect() as conn:
            for start_date, end_date in windows:
                for naics in naics_codes:
                    completed = JOB["done"] + 1
                    job_message(f"Searching NAICS {naics} · {start_date:%b %d}–{end_date:%b %d} ({completed}/{total})")
                    items = fetch_window(settings, start_date, end_date, naics, requests)
                    outcome = ingest(conn, items, settings)
                    for key in results:
                        results[key] += outcome[key]
                    conn.commit()
                    job_update(done=completed)
            state_set(conn, "last_successful_sync", now_iso())
            if kind == "backfill":
                state_set(conn, "initial_backfill_complete", "true")
            conn.execute("""UPDATE sync_runs SET finished_at=?,status=?,requests=?,inserted=?,updated=?,unchanged=? WHERE id=?""",
                         (now_iso(), "complete", requests[0], results["inserted"], results["updated"], results["unchanged"], run_id))
            conn.commit()
        text = f"Complete: {results['inserted']} new, {results['updated']} updated, {results['unchanged']} unchanged ({requests[0]} SAM requests)."
        job_update(running=False, finished_at=now_iso(), message=text, result=text)
    except Exception as error:
        message = f"Sync stopped: {error}"
        with connect() as conn:
            conn.execute("UPDATE sync_runs SET finished_at=?,status=?,requests=?,inserted=?,updated=?,unchanged=?,error=? WHERE id=?",
                         (now_iso(), "failed", requests[0], results["inserted"], results["updated"], results["unchanged"], str(error), run_id))
            conn.commit()
        job_update(running=False, finished_at=now_iso(), message=message, result=message)


def rescore_all(settings: dict) -> int:
    changed = 0
    with connect() as conn:
        rows = conn.execute("SELECT * FROM opportunities").fetchall()
        for row in rows:
            mutable = dict(row)
            mutable["agency_code"] = resolved_agency(mutable, settings)
            score, reasons = score_row(mutable, settings)
            conn.execute("UPDATE opportunities SET agency_code=?,fit_score=?,fit_reasons=?,changed_at=? WHERE notice_id=?",
                         (mutable["agency_code"], score, json.dumps(reasons), now_iso(), row["notice_id"]))
            changed += 1
        conn.commit()
    return changed


def rows_for_filters(params: dict, settings: dict, limit: int = 500) -> list[dict]:
    where = ["1=1"]
    values = []
    text = params.get("q", "").strip()
    if text:
        where.append("(title LIKE ? OR agency_name LIKE ? OR description LIKE ? OR naics_code LIKE ?)")
        values.extend([f"%{text}%"] * 4)
    if params.get("agency"):
        where.append("agency_code = ?")
        values.append(params["agency"])
    if params.get("type"):
        where.append("notice_type_code = ?")
        values.append(params["type"])
    try:
        minimum = max(0, int(params.get("min_score", 0)))
    except ValueError:
        minimum = 0
    where.append("fit_score >= ?")
    values.append(minimum)
    sort = params.get("sort", "fit")
    ordering = {
        "fit": "fit_score DESC, response_deadline ASC, posted_date DESC",
        "deadline": "CASE WHEN response_deadline = '' THEN 1 ELSE 0 END, response_deadline ASC, fit_score DESC",
        "newest": "posted_date DESC, fit_score DESC",
    }.get(sort, "fit_score DESC, response_deadline ASC")
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM opportunities WHERE {' AND '.join(where)} ORDER BY {ordering} LIMIT ?", (*values, limit)).fetchall()
    return [serialise_row(row) for row in rows]


def days_until(date_text: str) -> int | None:
    try:
        return (dt.date.fromisoformat(date_text) - dt.date.today()).days
    except (ValueError, TypeError):
        return None


def serialise_row(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    try:
        item["fit_reasons"] = json.loads(item.get("fit_reasons") or "[]")
    except json.JSONDecodeError:
        item["fit_reasons"] = []
    item["days_until_due"] = days_until(item.get("response_deadline", ""))
    return item


def stats(settings: dict) -> dict:
    minimum = int(settings["score"].get("min_alert_score", 4))
    today = dt.date.today()
    week = (today - dt.timedelta(days=7)).isoformat()
    due7 = (today + dt.timedelta(days=7)).isoformat()
    with connect() as conn:
        active = conn.execute("SELECT COUNT(*) FROM opportunities WHERE fit_score >= ?", (minimum,)).fetchone()[0]
        new_week = conn.execute("SELECT COUNT(*) FROM opportunities WHERE fit_score >= ? AND first_seen_at >= ?", (minimum, week)).fetchone()[0]
        due = conn.execute("SELECT COUNT(*) FROM opportunities WHERE fit_score >= ? AND response_deadline BETWEEN ? AND ?", (minimum, today.isoformat(), due7)).fetchone()[0]
        agencies = conn.execute("SELECT COUNT(DISTINCT agency_code) FROM opportunities WHERE fit_score >= ? AND agency_code != ''", (minimum,)).fetchone()[0]
        agency_rows = conn.execute("SELECT agency_code, COUNT(*) AS count FROM opportunities WHERE fit_score >= ? AND agency_code != '' GROUP BY agency_code ORDER BY count DESC", (minimum,)).fetchall()
        last_run = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return {"active": active, "new_week": new_week, "due_7": due, "agencies": agencies,
            "by_agency": [dict(row) for row in agency_rows], "last_sync": dict(last_run) if last_run else None}


def digest_candidates(settings: dict) -> tuple[list[dict], list[dict], list[dict]]:
    min_score = int(settings["score"].get("min_alert_score", 4))
    today = dt.date.today()
    week = (today + dt.timedelta(days=7)).isoformat()
    two_days = (today + dt.timedelta(days=2)).isoformat()
    three_days = (today + dt.timedelta(days=3)).isoformat()
    with connect() as conn:
        new_rows = conn.execute("""SELECT o.* FROM opportunities o LEFT JOIN notifications n
                                 ON n.notice_id=o.notice_id AND n.reason='new'
                                 WHERE o.fit_score >= ? AND n.notice_id IS NULL ORDER BY o.fit_score DESC, o.posted_date DESC""", (min_score,)).fetchall()
        seven = conn.execute("""SELECT o.* FROM opportunities o LEFT JOIN notifications n
                              ON n.notice_id=o.notice_id AND n.reason='due_7'
                              WHERE o.fit_score >= ? AND o.response_deadline BETWEEN ? AND ? AND n.notice_id IS NULL
                              ORDER BY o.response_deadline""", (min_score, three_days, week)).fetchall()
        two = conn.execute("""SELECT o.* FROM opportunities o LEFT JOIN notifications n
                            ON n.notice_id=o.notice_id AND n.reason='due_2'
                            WHERE o.fit_score >= ? AND o.response_deadline BETWEEN ? AND ? AND n.notice_id IS NULL
                            ORDER BY o.response_deadline""", (min_score, today.isoformat(), two_days)).fetchall()
    return [serialise_row(row) for row in new_rows], [serialise_row(row) for row in seven], [serialise_row(row) for row in two]


def digest_html(settings: dict) -> tuple[str, list[tuple[str, str]]]:
    new_rows, seven, two = digest_candidates(settings)
    sections = [("New matches", "new", new_rows), ("Due within 2 days", "due_2", two), ("Due within 7 days", "due_7", seven)]
    parts = ["<div style='font-family:Arial,sans-serif;max-width:760px;color:#202024'>",
             "<h1 style='color:#5B2D86'>SAM Radar daily brief</h1>"]
    marks = []
    anything = False
    for heading, reason, rows in sections:
        if not rows:
            continue
        anything = True
        parts.append(f"<h2>{html.escape(heading)} <span style='color:#777'>({len(rows)})</span></h2><ul>")
        for row in rows:
            title = html.escape(row["title"])
            link = html.escape(row["sam_url"] or "#", quote=True)
            agency = html.escape(row["agency_code"] or row["agency_name"] or "Agency")
            due = html.escape(row["response_deadline"] or "No response date listed")
            reasons = html.escape(" · ".join(row["fit_reasons"][:3]))
            parts.append(f"<li style='margin:0 0 14px'><a href='{link}' style='color:#5B2D86;font-weight:bold'>{title}</a><br>"
                         f"Score {row['fit_score']} · {agency} · Due {due}<br><span style='color:#666'>{reasons}</span></li>")
            marks.append((row["notice_id"], reason))
        parts.append("</ul>")
    if not anything:
        parts.append("<p>No new qualified matches or deadline reminders today. SAM Radar checked successfully.</p>")
    parts.append("<p style='color:#777;font-size:12px'>SAM Radar · explainable opportunity intelligence</p></div>")
    return "".join(parts), marks


def send_email(settings: dict, subject: str, body: str) -> None:
    email_conf = settings["email"]
    recipients = email_conf.get("to_addresses") or []
    if not (email_conf.get("enabled") and email_conf.get("smtp_username") and email_conf.get("smtp_password") and recipients):
        raise RuntimeError("Configure email, a username, an app password, and at least one recipient first.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_conf.get("from_address") or email_conf["smtp_username"]
    message["To"] = ", ".join(recipients)
    message.set_content("SAM Radar daily brief. Open this email in an HTML-capable mail client.")
    message.add_alternative(body, subtype="html")
    with smtplib.SMTP(email_conf["smtp_host"], int(email_conf["smtp_port"]), timeout=30) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(email_conf["smtp_username"], email_conf["smtp_password"])
        client.send_message(message)


def mark_sent(marks: list[tuple[str, str]]) -> None:
    with connect() as conn:
        conn.executemany("INSERT OR IGNORE INTO notifications(notice_id,reason,sent_at) VALUES(?,?,?)",
                         [(notice_id, reason, now_iso()) for notice_id, reason in marks])
        conn.commit()


def send_digest_now() -> str:
    settings = load_settings()
    body, marks = digest_html(settings)
    send_email(settings, "SAM Radar — daily opportunity brief", body)
    mark_sent(marks)
    return f"Digest sent ({len(marks)} opportunity notifications recorded)."


def record_auto_result(text: str) -> None:
    settings = load_settings()
    settings["email"]["last_auto_at"] = now_iso()
    settings["email"]["last_auto_result"] = text
    save_settings(settings)


def morning_routine() -> str:
    """Sync then send.  It is synchronous only inside its background thread."""
    started, message = start_sync("incremental")
    if not started:
        # A just-started scheduled/manual sync is still useful work. Wait for it
        # instead of sending stale data or silently skipping the digest.
        with JOB_LOCK:
            if not JOB["running"]:
                return message
    while True:
        with JOB_LOCK:
            running = JOB["running"]
            result = JOB["result"]
        if not running:
            break
        time.sleep(1)
    if result.startswith("Sync stopped") or result == "No API key":
        return result
    try:
        digest_result = send_digest_now()
        return f"{result} {digest_result}"
    except Exception as error:
        return f"{result} Email not sent: {error}"


def scheduler_loop() -> None:
    last_sync_day = ""
    last_email_day = ""
    while True:
        try:
            settings = load_settings()
            now = dt.datetime.now()
            today = now.date().isoformat()
            # When email automation is on, morning_routine owns the sync so the
            # digest cannot race a separate scheduled sync.
            if settings["sync"].get("auto_sync") and not settings["email"].get("auto_send") and now.strftime("%H:%M") >= settings["sync"].get("daily_sync_time", "07:30") and last_sync_day != today:
                last_sync_day = today
                start_sync("incremental")
            if settings["email"].get("auto_send") and now.strftime("%H:%M") >= settings["email"].get("send_time", "08:00") and last_email_day != today:
                last_email_day = today  # set first: a bad credential must not create retry storms
                def send_background():
                    outcome = morning_routine()
                    record_auto_result(outcome)
                threading.Thread(target=send_background, daemon=True, name="sam-radar-digest").start()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


def request_data(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 1_000_000:
        raise ValueError("Request is too large.")
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError("Body must be JSON.") from error


class Handler(BaseHTTPRequestHandler):
    server_version = "SAMRadar/2.0"

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def authenticated(self) -> bool:
        password = load_settings()["app"].get("access_password", "")
        if not password:
            return True
        expected = "Basic " + base64.b64encode(("radar:" + password).encode()).decode()
        presented = self.headers.get("Authorization", "")
        if secrets.compare_digest(presented, expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.security_headers()
        self.send_header("WWW-Authenticate", 'Basic realm="SAM Radar"')
        self.end_headers()
        return False

    def send_json(self, payload, status=200):
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error_json(self, message, status=400):
        self.send_json({"ok": False, "error": message}, status)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        # Hosts use this endpoint to decide whether to route traffic. It leaks no
        # customer data and remains available when the optional password wall is on.
        if parsed.path == "/healthz":
            self.send_json({"ok": True, "status": "healthy"})
            return
        if not self.authenticated():
            return
        params = {key: value[-1] for key, value in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if parsed.path in ("/", "/index.html"):
                body = INDEX_PATH.read_bytes()
                self.send_response(200)
                self.security_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/state":
                settings = load_settings()
                try:
                    lan = socket.gethostbyname(socket.gethostname())
                except socket.gaierror:
                    lan = "127.0.0.1"
                self.send_json({"ok": True, "settings": public_settings(settings), "stats": stats(settings), "job": JOB,
                                "lan_url": f"http://{lan}:{settings['app']['port']}", "sample_mode": not bool(settings["sam"].get("api_key"))})
            elif parsed.path == "/api/opportunities":
                self.send_json({"ok": True, "items": rows_for_filters(params, load_settings())})
            elif parsed.path == "/api/job":
                self.send_json({"ok": True, "job": JOB})
            elif parsed.path == "/api/email/preview":
                body, marks = digest_html(load_settings())
                self.send_json({"ok": True, "html": body, "notification_count": len(marks)})
            elif parsed.path == "/api/export.csv":
                rows = rows_for_filters(params, load_settings(), limit=5000)
                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(["Fit score", "Title", "Agency", "Notice type", "NAICS", "Set-aside", "Posted", "Due", "SAM URL", "Fit reasons"])
                for row in rows:
                    writer.writerow([row["fit_score"], row["title"], row["agency_code"] or row["agency_name"], row["notice_type"],
                                     row["naics_code"], row["set_aside"], row["posted_date"], row["response_deadline"], row["sam_url"],
                                     " | ".join(row["fit_reasons"])])
                data = output.getvalue().encode("utf-8")
                self.send_response(200)
                self.security_headers()
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="sam-radar-opportunities.csv"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error_json("Not found.", 404)
        except Exception as error:
            traceback.print_exc()
            self.send_error_json(str(error), 500)

    def do_POST(self):
        if not self.authenticated():
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            data = request_data(self)
            if path == "/api/sync":
                kind = "backfill" if data.get("kind") == "backfill" else "incremental"
                days = int(data["days"]) if data.get("days") else None
                ok, message = start_sync(kind, days)
                self.send_json({"ok": ok, "message": message}, 202 if ok else 409)
            elif path == "/api/settings":
                current = load_settings()
                merged = deep_merge(current, data)
                # Empty secret fields from the UI mean “unchanged,” never erase a working deployment secret.
                for section, key in (("sam", "api_key"), ("app", "access_password"), ("email", "smtp_password")):
                    if data.get(section, {}).get(key, None) in ("", "configured", None):
                        merged[section][key] = current[section][key]
                save_settings(merged)
                count = rescore_all(merged)
                self.send_json({"ok": True, "message": f"Settings saved; rescored {count} opportunities.", "settings": public_settings(merged)})
            elif path == "/api/email/send":
                result = send_digest_now()
                self.send_json({"ok": True, "message": result})
            elif path == "/api/email/run-now":
                with JOB_LOCK:
                    if JOB["running"]:
                        self.send_error_json("A sync is already running.", 409)
                        return
                def run_routine():
                    result = morning_routine()
                    record_auto_result(result)
                threading.Thread(target=run_routine, daemon=True, name="sam-radar-run-now").start()
                self.send_json({"ok": True, "message": "Morning routine started: syncing first, then sending the digest."}, 202)
            else:
                self.send_error_json("Not found.", 404)
        except (ValueError, RuntimeError) as error:
            self.send_error_json(str(error), 400)
        except Exception as error:
            traceback.print_exc()
            self.send_error_json(str(error), 500)


def main():
    init_db()
    settings = load_settings()
    if not SETTINGS_PATH.exists():
        save_settings(settings)
    threading.Thread(target=scheduler_loop, daemon=True, name="sam-radar-scheduler").start()
    port = int(os.getenv("PORT", settings["app"].get("port", 8765)))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    local_url = f"http://127.0.0.1:{port}"
    print(f"SAM Radar is running at {local_url}")
    if settings["app"].get("public_url"):
        print(f"Public URL: {settings['app']['public_url']}")
    if settings["app"].get("open_browser") and not os.getenv("RENDER") and not os.getenv("RAILWAY_ENVIRONMENT"):
        threading.Timer(0.6, lambda: webbrowser.open(local_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping SAM Radar.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
