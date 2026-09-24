"""SQLite persistence. The Repository is the only module that talks SQL.

Models in / models out: callers never see rows. Updates take a full (immutable) model and
persist it — build the new version with ``model.model_copy(update=...)``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jobbot import clock
from jobbot.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    ATSReport,
    Company,
    ContactEmail,
    JobOffer,
    JobStatus,
    SentEmail,
    TailoredDocuments,
)
from jobbot.textutil import dedup_key, normalize_company

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL, title TEXT NOT NULL,
  company TEXT, location TEXT, postal_code TEXT, latitude REAL, longitude REAL, description TEXT,
  url TEXT, apply_url TEXT, apply_method TEXT, contract_type TEXT, remote INTEGER, salary TEXT,
  contact_email TEXT, contact_name TEXT, company_domain TEXT, published_at TEXT, found_at TEXT NOT NULL,
  status TEXT NOT NULL, match_score REAL, match_reasons TEXT, raw TEXT, dedup_key TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);

CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, siren TEXT, naf_code TEXT, naf_label TEXT, headcount_range TEXT,
  city TEXT, postal_code TEXT, address TEXT, latitude REAL, longitude REAL, domain TEXT, website TEXT,
  careers_url TEXT, description TEXT, leaders TEXT, contacts TEXT, source TEXT, found_at TEXT NOT NULL, status TEXT NOT NULL,
  raw TEXT, name_key TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);
CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name_key);

CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, company_id TEXT, cv_pdf TEXT NOT NULL, cv_docx TEXT,
  letter_pdf TEXT, letter_text TEXT, email_subject TEXT, email_body TEXT, cv_ats TEXT, letter_ats TEXT,
  truth_warnings TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_job ON documents(job_id);
CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, company_id TEXT, method TEXT NOT NULL,
  status TEXT NOT NULL, applied_at TEXT NOT NULL, company_name TEXT, company_key TEXT, job_title TEXT,
  email_to TEXT, email_message_id TEXT, email_thread_ids TEXT, cv_path TEXT, letter_path TEXT,
  followup_count INTEGER NOT NULL DEFAULT 0, last_contact_at TEXT, next_followup_at TEXT,
  last_response_at TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_app_company ON applications(company_key);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL, at TEXT NOT NULL,
  kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id);

CREATE TABLE IF NOT EXISTS sent_emails (
  id INTEGER PRIMARY KEY AUTOINCREMENT, to_addr TEXT NOT NULL, subject TEXT, message_id TEXT UNIQUE,
  in_reply_to TEXT, kind TEXT NOT NULL, application_id INTEGER, sent_at TEXT NOT NULL, dry_run INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL,
  ok INTEGER NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_channel ON action_log(channel, action, at);

CREATE TABLE IF NOT EXISTS blacklist (value TEXT PRIMARY KEY, reason TEXT, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

_JOB_COLS = ["id", "source", "external_id", "title", "company", "location", "postal_code", "latitude", "longitude", "description", "url", "apply_url", "apply_method", "contract_type", "remote", "salary", "contact_email", "contact_name", "company_domain", "published_at", "found_at", "status", "match_score", "match_reasons", "raw"]
_JOB_JSON = {"match_reasons", "raw"}

_COMPANY_COLS = ["id", "name", "siren", "naf_code", "naf_label", "headcount_range", "city", "postal_code", "address", "latitude", "longitude", "domain", "website", "careers_url", "description", "leaders", "contacts", "source", "found_at", "status", "raw"]
_COMPANY_JSON = {"leaders", "contacts", "raw"}

_APP_COLS = ["id", "job_id", "company_id", "method", "status", "applied_at", "company_name", "job_title", "email_to", "email_message_id", "email_thread_ids", "cv_path", "letter_path", "followup_count", "last_contact_at", "next_followup_at", "last_response_at", "notes"]
_APP_JSON = {"email_thread_ids"}


def _to_row(model: Any, cols: list[str], json_cols: set[str]) -> dict[str, Any]:
    """Model → row. Datetimes use one canonical ISO format so SQL string comparisons are valid."""
    data = model.model_dump(mode="json")
    row: dict[str, Any] = {}
    for col in cols:
        attr = getattr(model, col, None)
        if col in json_cols:
            row[col] = json.dumps(data.get(col), ensure_ascii=False)
        elif isinstance(attr, datetime):
            row[col] = clock.iso(attr)
        elif isinstance(attr, bool):
            row[col] = int(attr)
        else:
            row[col] = data.get(col)
    return row


def _from_row(row: sqlite3.Row, cols: list[str], json_cols: set[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for col in cols:
        val = row[col]
        if col in json_cols:
            val = json.loads(val) if val else None
            if val is None:
                continue  # let the model default apply
        if val is None:
            continue
        data[col] = val
    return data


class Repository:
    def __init__(self, db_path: Path | str):
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    # ------------------------------------------------------------------ jobs

    def upsert_job(self, job: JobOffer) -> bool:
        """Insert a new job. If it exists, refresh descriptive fields only (never status/score).

        Empty values never overwrite stored ones, and ``raw`` is merged (keys only known from a detail page —
        LBA ``recipient_id``, HelloWork response type… — survive a later search/alert refresh). A minimal offer
        rebuilt from an alert e-mail (``raw["from_alert"]``) only adds the keys that are missing.

        Returns True when the job is new.
        """
        row = _to_row(job, _JOB_COLS, _JOB_JSON)
        row["dedup_key"] = dedup_key(job.company, job.title)
        row["updated_at"] = clock.iso(clock.now())
        existing = self._query("SELECT id, title, company, description, raw FROM jobs WHERE id = ?", [job.id])
        with self._tx() as conn:
            if not existing:
                cols = ", ".join(row)
                marks = ", ".join(f":{c}" for c in row)
                conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", row)
                return True
            old = existing[0]
            fresh_raw = json.loads(row["raw"]) or {}  # JSON-safe copy of job.raw
            refresh: dict[str, Any] = {"updated_at": row["updated_at"], "raw": _merge_raw(old["raw"], fresh_raw)}
            for key in ("title", "company", "location", "url", "apply_url", "salary",
                        "contact_email", "contact_name", "company_domain", "latitude", "longitude"):
                if row[key] not in (None, "") and not job.raw.get("from_alert"):
                    refresh[key] = row[key]
            if len(job.description or "") > len(old["description"] or ""):
                refresh["description"] = row["description"]
            refresh["dedup_key"] = dedup_key(refresh.get("company", old["company"]) or "",
                                             refresh.get("title", old["title"]) or "")
            sets = ", ".join(f"{k} = :{k}" for k in refresh)
            conn.execute(f"UPDATE jobs SET {sets} WHERE id = :id", {**refresh, "id": job.id})
            return False

    def save_job(self, job: JobOffer) -> None:
        """Persist every field of ``job`` (full replace)."""
        row = _to_row(job, _JOB_COLS, _JOB_JSON)
        row["dedup_key"] = dedup_key(job.company, job.title)
        row["updated_at"] = clock.iso(clock.now())
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._tx() as conn:
            conn.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({marks})", row)

    def get_job(self, job_id: str) -> JobOffer | None:
        rows = self._query("SELECT * FROM jobs WHERE id = ?", [job_id])
        return JobOffer.model_validate(_from_row(rows[0], _JOB_COLS, _JOB_JSON)) if rows else None

    def list_jobs(
        self,
        status: JobStatus | Iterable[JobStatus] | None = None,
        source: str | None = None,
        limit: int | None = None,
        order: str = "match_score DESC, found_at DESC",
    ) -> list[JobOffer]:
        where, params = [], []
        if status is not None:
            statuses = [status] if isinstance(status, JobStatus) else list(status)
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(s.value for s in statuses)
        if source:
            where.append("source = ?")
            params.append(source)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if order not in {"match_score DESC, found_at DESC", "found_at DESC", "published_at DESC"}:
            raise ValueError(f"unsupported order: {order}")
        sql += f" ORDER BY {order}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [JobOffer.model_validate(_from_row(r, _JOB_COLS, _JOB_JSON)) for r in self._query(sql, params)]

    def update_job_status(self, job_id: str, status: JobStatus) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                [status.value, clock.iso(clock.now()), job_id],
            )

    def set_job_match(self, job_id: str, score: float, reasons: list[str], status: JobStatus) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET match_score = ?, match_reasons = ?, status = ?, updated_at = ? WHERE id = ?",
                [score, json.dumps(reasons, ensure_ascii=False), status.value, clock.iso(clock.now()), job_id],
            )

    def find_duplicate_jobs(self, job: JobOffer) -> list[JobOffer]:
        """Other jobs (different id) that look like the same offer on another board."""
        rows = self._query(
            "SELECT * FROM jobs WHERE dedup_key = ? AND id != ?", [dedup_key(job.company, job.title), job.id]
        )
        return [JobOffer.model_validate(_from_row(r, _JOB_COLS, _JOB_JSON)) for r in rows]

    # ------------------------------------------------------------------ companies

    def upsert_company(self, company: Company) -> bool:
        """Insert or fully replace a company. Returns True when new."""
        row = _to_row(company, _COMPANY_COLS, _COMPANY_JSON)
        row["name_key"] = normalize_company(company.name)
        row["updated_at"] = clock.iso(clock.now())
        is_new = not self._query("SELECT 1 FROM companies WHERE id = ?", [company.id])
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._tx() as conn:
            conn.execute(f"INSERT OR REPLACE INTO companies ({cols}) VALUES ({marks})", row)
        return is_new

    def get_company(self, company_id: str) -> Company | None:
        rows = self._query("SELECT * FROM companies WHERE id = ?", [company_id])
        return _company(rows[0]) if rows else None

    def list_companies(self, status: str | Iterable[str] | None = None, limit: int | None = None) -> list[Company]:
        sql, params = "SELECT * FROM companies", []
        if status is not None:
            statuses = [status] if isinstance(status, str) else list(status)
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY found_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_company(r) for r in self._query(sql, params)]

    # ------------------------------------------------------------------ documents

    def save_documents(self, docs: TailoredDocuments) -> int:
        data = docs.model_dump(mode="json")
        row = {
            "job_id": docs.job_id,
            "company_id": docs.company_id,
            "cv_pdf": docs.cv_pdf,
            "cv_docx": docs.cv_docx,
            "letter_pdf": docs.letter_pdf,
            "letter_text": docs.letter_text,
            "email_subject": docs.email_subject,
            "email_body": docs.email_body,
            "cv_ats": json.dumps(data["cv_ats"], ensure_ascii=False),
            "letter_ats": json.dumps(data["letter_ats"], ensure_ascii=False),
            "truth_warnings": json.dumps(docs.truth_warnings, ensure_ascii=False),
            "created_at": clock.iso(docs.created_at),
        }
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._tx() as conn:
            cur = conn.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", row)
            return int(cur.lastrowid)

    def latest_documents(self, job_id: str | None = None, company_id: str | None = None) -> TailoredDocuments | None:
        if job_id:
            rows = self._query("SELECT * FROM documents WHERE job_id = ? ORDER BY id DESC LIMIT 1", [job_id])
        elif company_id:
            rows = self._query("SELECT * FROM documents WHERE company_id = ? ORDER BY id DESC LIMIT 1", [company_id])
        else:
            raise ValueError("job_id or company_id required")
        if not rows:
            return None
        r = rows[0]
        return TailoredDocuments(
            job_id=r["job_id"],
            company_id=r["company_id"],
            cv_pdf=r["cv_pdf"],
            cv_docx=r["cv_docx"],
            letter_pdf=r["letter_pdf"],
            letter_text=r["letter_text"] or "",
            email_subject=r["email_subject"] or "",
            email_body=r["email_body"] or "",
            cv_ats=_ats(r["cv_ats"]),
            letter_ats=_ats(r["letter_ats"]),
            truth_warnings=json.loads(r["truth_warnings"] or "[]"),
            created_at=clock.parse_dt(r["created_at"]),
        )

    # ------------------------------------------------------------------ applications

    def create_application(self, app: Application) -> Application:
        row = _to_row(app, _APP_COLS, _APP_JSON)
        row.pop("id")
        row["company_key"] = normalize_company(app.company_name)
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._tx() as conn:
            cur = conn.execute(f"INSERT INTO applications ({cols}) VALUES ({marks})", row)
            new_id = int(cur.lastrowid)
        return app.model_copy(update={"id": new_id})

    def update_application(self, app: Application) -> None:
        if app.id is None:
            raise ValueError("application has no id")
        row = _to_row(app, _APP_COLS, _APP_JSON)
        row["company_key"] = normalize_company(app.company_name)
        sets = ", ".join(f"{k} = :{k}" for k in row if k != "id")
        with self._tx() as conn:
            conn.execute(f"UPDATE applications SET {sets} WHERE id = :id", row)

    def get_application(self, app_id: int) -> Application | None:
        rows = self._query("SELECT * FROM applications WHERE id = ?", [app_id])
        return _application(rows[0]) if rows else None

    def list_applications(
        self, status: ApplicationStatus | Iterable[ApplicationStatus] | None = None, limit: int | None = None
    ) -> list[Application]:
        sql, params = "SELECT * FROM applications", []
        if status is not None:
            statuses = [status] if isinstance(status, ApplicationStatus) else list(status)
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY applied_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_application(r) for r in self._query(sql, params)]

    def find_application_by_message_id(self, message_id: str) -> Application | None:
        """Match a Message-ID against the first email or any follow-up of a thread."""
        mid = message_id.strip()
        rows = self._query(
            "SELECT * FROM applications WHERE email_message_id = ? OR email_thread_ids LIKE ?",
            [mid, f'%"{mid}"%'],
        )
        return _application(rows[0]) if rows else None

    def find_applications_by_email(self, address: str) -> list[Application]:
        rows = self._query(
            "SELECT * FROM applications WHERE lower(email_to) = ? ORDER BY applied_at DESC", [address.lower()]
        )
        return [_application(r) for r in rows]

    def application_for_job(self, job_id: str) -> Application | None:
        rows = self._query("SELECT * FROM applications WHERE job_id = ? LIMIT 1", [job_id])
        return _application(rows[0]) if rows else None

    def recently_applied_to_company(self, company_name: str, within_days: int = 60) -> bool:
        key = normalize_company(company_name)
        if not key:
            return False
        since = clock.iso(clock.now() - timedelta(days=within_days))
        rows = self._query(
            "SELECT 1 FROM applications WHERE company_key = ? AND applied_at >= ? LIMIT 1", [key, since]
        )
        return bool(rows)

    def due_followups(self, at: datetime | None = None) -> list[Application]:
        ts = clock.iso(at or clock.now())
        rows = self._query(
            "SELECT * FROM applications WHERE next_followup_at IS NOT NULL AND next_followup_at <= ? "
            "AND status IN (?, ?) ORDER BY next_followup_at",
            [ts, ApplicationStatus.APPLIED.value, ApplicationStatus.FOLLOWED_UP.value],
        )
        return [_application(r) for r in rows]

    # ------------------------------------------------------------------ events

    def add_event(self, event: ApplicationEvent) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO events (application_id, at, kind, detail) VALUES (?, ?, ?, ?)",
                [event.application_id, clock.iso(event.at), event.kind, event.detail],
            )

    def list_events(self, application_id: int) -> list[ApplicationEvent]:
        rows = self._query("SELECT * FROM events WHERE application_id = ? ORDER BY at", [application_id])
        return [
            ApplicationEvent(
                id=r["id"], application_id=r["application_id"], at=clock.parse_dt(r["at"]),
                kind=r["kind"], detail=r["detail"] or "",
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ emails & rate limits

    def log_sent_email(self, email: SentEmail) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO sent_emails (to_addr, subject, message_id, in_reply_to, kind, application_id, "
                "sent_at, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [email.to, email.subject, email.message_id, email.in_reply_to, email.kind,
                 email.application_id, clock.iso(email.sent_at), int(email.dry_run)],
            )

    def count_sent_emails_since(self, since: datetime, kind: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) FROM sent_emails WHERE sent_at >= ? AND dry_run = 0", [clock.iso(since)]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return int(self._query(sql, params)[0][0])

    def all_sent_message_ids(self) -> set[str]:
        return {r[0] for r in self._query("SELECT message_id FROM sent_emails WHERE dry_run = 0")}

    def log_action(self, channel: str, action: str, ok: bool, detail: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO action_log (channel, action, at, ok, detail) VALUES (?, ?, ?, ?, ?)",
                [channel, action, clock.iso(clock.now()), int(ok), detail],
            )

    def count_actions_since(self, channel: str, action: str, since: datetime, ok_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM action_log WHERE channel = ? AND action = ? AND at >= ?"
        if ok_only:
            sql += " AND ok = 1"
        return int(self._query(sql, [channel, action, clock.iso(since)])[0][0])

    def count_actions_today(self, channel: str, action: str) -> int:
        today = clock.to_paris(clock.now()).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.count_actions_since(channel, action, today)

    # ------------------------------------------------------------------ blacklist & kv

    def blacklist_add(self, value: str, reason: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO blacklist (value, reason, added_at) VALUES (?, ?, ?)",
                [value.strip().lower(), reason, clock.iso(clock.now())],
            )

    def is_blacklisted(self, *values: str) -> bool:
        keys = [v.strip().lower() for v in values if v and v.strip()]
        if not keys:
            return False
        rows = self._query(f"SELECT 1 FROM blacklist WHERE value IN ({','.join('?' * len(keys))}) LIMIT 1", keys)
        return bool(rows)

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT value FROM kv WHERE key = ?", [key])
        return rows[0]["value"] if rows else default

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", [key, value])

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict[str, Any]:
        def counts(sql: str) -> dict[str, int]:
            return {r[0]: r[1] for r in self._query(sql)}

        return {
            "jobs_by_status": counts("SELECT status, COUNT(*) FROM jobs GROUP BY status"),
            "jobs_by_source": counts("SELECT source, COUNT(*) FROM jobs GROUP BY source"),
            "applications_by_status": counts("SELECT status, COUNT(*) FROM applications GROUP BY status"),
            "applications_by_method": counts("SELECT method, COUNT(*) FROM applications GROUP BY method"),
            "companies_by_status": counts("SELECT status, COUNT(*) FROM companies GROUP BY status"),
            "emails_sent": int(self._query("SELECT COUNT(*) FROM sent_emails WHERE dry_run = 0")[0][0]),
        }


def _merge_raw(stored: str | None, fresh: dict[str, Any]) -> str:
    """Stored ``raw`` refreshed with the new non-null values (alert offers only fill the missing keys)."""
    try:
        old = json.loads(stored) if stored else {}
    except ValueError:
        old = {}
    old = old if isinstance(old, dict) else {}
    if fresh.get("from_alert"):
        merged = {**old, **{k: v for k, v in fresh.items() if k not in old}}
    else:
        merged = {**old, **{k: v for k, v in fresh.items() if v is not None}}
    return json.dumps(merged, ensure_ascii=False)


def _ats(raw: str | None) -> ATSReport | None:
    data = json.loads(raw) if raw else None
    return ATSReport.model_validate(data) if data else None


def _company(row: sqlite3.Row) -> Company:
    data = _from_row(row, _COMPANY_COLS, _COMPANY_JSON)
    if "contacts" in data:
        data["contacts"] = [ContactEmail.model_validate(c) for c in data["contacts"]]
    return Company.model_validate(data)


def _application(row: sqlite3.Row) -> Application:
    return Application.model_validate(_from_row(row, _APP_COLS, _APP_JSON))
