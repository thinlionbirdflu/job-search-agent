"""SQLite storage with stable job IDs and independent application status."""

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from .models import Job


STATUSES = ("new", "saved", "applied", "interviewing", "offer", "closed", "skipped")
MINIMUM_FIT = 60
ATS_OVERRIDE = 80
STATES = ("eligible", "review", "rejected", "pending", "excluded")
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    priority INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_seen TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending',
    checks TEXT,
    fit TEXT,
    score REAL
);
CREATE TABLE IF NOT EXISTS aliases (
    identity TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id)
);
"""


class IdentityConflict(ValueError):
    pass


def decide_state(eligibility_state: str, score, ats_score) -> str:
    # A strong ATS keyword match can qualify a job even when its fit score is below the threshold.
    passes_ats = ats_score is not None and ats_score > ATS_OVERRIDE
    if eligibility_state != "excluded" and not passes_ats:
        if score is None:
            return "review"
        if score < MINIMUM_FIT:
            return "rejected"
    return eligibility_state


class Tracker:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._add_score_columns()
        self._upgrade_classifications()

    def _add_score_columns(self):
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        with self.db:
            for name in ("local_score", "ai_score"):
                if name not in existing:
                    self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} REAL")

    def _upgrade_classifications(self):
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
                return
            self.db.execute("UPDATE jobs SET state = 'excluded' WHERE state = 'rejected'")
            self.db.execute(
                "UPDATE jobs SET state = 'rejected' WHERE state IN ('eligible', 'review') AND score < ?",
                (MINIMUM_FIT,),
            )
            self.db.execute("UPDATE jobs SET state = 'review' WHERE state = 'eligible' AND score IS NULL")
            self.db.execute("PRAGMA user_version = 1")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def ingest(self, records: list[tuple[Job, int]]) -> tuple[list[sqlite3.Row], list[str]]:
        ids = set()
        conflicts = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for job, priority in records:
                try:
                    ids.add(self._upsert(job, priority))
                except IdentityConflict as exc:
                    conflicts.append(str(exc))
            # One round trip for every touched row, not one query per id: ingest() can touch
            # hundreds to thousands of rows in a single live-board run.
            ordered = sorted(ids)
            if ordered:
                placeholders = ",".join("?" * len(ordered))
                by_id = {row["id"]: row for row in
                         self.db.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", ordered)}
                rows = [by_id[job_id] for job_id in ordered]
            else:
                rows = []
        return rows, conflicts

    def _upsert(self, job: Job, priority: int) -> int:
        identities = job.identity_keys()
        existing = self.db.execute(
            "SELECT DISTINCT job_id FROM aliases WHERE identity IN (?, ?)", identities
        ).fetchall()
        if len(existing) > 1:
            raise IdentityConflict(f"{job.source}/{job.source_id} links tracked jobs "
                                   f"{existing[0]['job_id']} and {existing[1]['job_id']}")
        payload = json.dumps(asdict(job), sort_keys=True, ensure_ascii=False)
        if not existing:
            cursor = self.db.execute(
                "INSERT INTO jobs (payload, priority) VALUES (?, ?)", (payload, priority)
            )
            job_id = cursor.lastrowid
        else:
            job_id = existing[0]["job_id"]
            current = self.db.execute("SELECT payload, priority FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if priority >= current["priority"] and payload != current["payload"]:
                self.db.execute(
                    """UPDATE jobs SET payload = ?, priority = ?, revision = revision + 1,
                       state = 'pending', checks = NULL, fit = NULL, score = NULL WHERE id = ?""",
                    (payload, priority, job_id),
                )
            elif priority > current["priority"]:
                self.db.execute("UPDATE jobs SET priority = ? WHERE id = ?", (priority, job_id))
            self.db.execute(
                "UPDATE jobs SET last_seen = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?", (job_id,)
            )
        self.db.executemany("INSERT OR IGNORE INTO aliases VALUES (?, ?)",
                            [(identity, job_id) for identity in identities])
        return job_id

    def save_evaluation(self, job_id, revision, eligibility, fit, local_score=None) -> str | None:
        score = fit.score if fit else None
        ats_score = fit.ats.score if fit and fit.ats else None
        state = decide_state(eligibility.state, score, ats_score)
        method = fit.method if fit else None
        ai_score = score if method == "ai" else None
        if method != "ai":
            # Local scoring or a failed-AI fallback: the fit is the local score.
            local_score = score
        with self.db:
            result = self.db.execute(
                """UPDATE jobs SET state = ?, checks = ?, fit = ?, score = ?,
                   local_score = ?, ai_score = ?
                   WHERE id = ? AND revision = ?""",
                (state, json.dumps(asdict(eligibility)["checks"]),
                 json.dumps(asdict(fit)) if fit else None, score, local_score, ai_score,
                 job_id, revision),
            )
        return state if result.rowcount == 1 else None

    def refresh_eligibility(self, job_id, revision, eligibility, stored_fit: dict) -> str | None:
        # Recompute eligibility and state while keeping the stored fit, so no model call is needed.
        score = stored_fit.get("score")
        ats_score = (stored_fit.get("ats") or {}).get("score")
        state = decide_state(eligibility.state, score, ats_score)
        with self.db:
            result = self.db.execute(
                "UPDATE jobs SET state = ?, checks = ? WHERE id = ? AND revision = ?",
                (state, json.dumps(asdict(eligibility)["checks"]), job_id, revision),
            )
        return state if result.rowcount == 1 else None

    def list_jobs(self, state: str, status: str | None, limit: int = -1):
        # No per-row payload deserialization here: is_live_us() is already baked into state
        # ("excluded" is terminal for it), so re-deriving it from the JSON on every row -- and
        # re-parsing the same JSON a second time in the caller to actually use the row -- bought
        # nothing but cost, on every list and every export.
        return self.db.execute(
            """SELECT * FROM jobs WHERE (? = 'all' OR state = ?) AND (? IS NULL OR status = ?)
               ORDER BY CASE state WHEN 'eligible' THEN 0 WHEN 'review' THEN 1
                        WHEN 'rejected' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,
                        score DESC, id LIMIT ?""",
            (state, state, status, status, limit),
        ).fetchall()

    def show(self, job_id: int) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown job ID {job_id}")
        result = dict(row)
        for field in ("checks", "fit"):
            if result[field] is not None:
                result[field] = json.loads(result[field])
        result["job"] = json.loads(result.pop("payload"))
        result["identities"] = [
            alias["identity"] for alias in self.db.execute(
                "SELECT identity FROM aliases WHERE job_id = ? ORDER BY identity", (job_id,)
            )
        ]
        return result

    def set_status(self, job_id: int, status: str, notes: str | None):
        with self.db:
            result = self.db.execute(
                "UPDATE jobs SET status = ?, notes = coalesce(?, notes) WHERE id = ?",
                (status, notes, job_id),
            )
        if result.rowcount == 0:
            raise ValueError(f"Unknown job ID {job_id}")
