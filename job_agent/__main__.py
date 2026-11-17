"""Command-line entry point."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys

from .config import Settings, load_env
from .filters import Check, Eligibility, evaluate
from .models import Job
from .scoring import RUBRICS, estimate_ats, local_fit, score_job


AI_ENV = {"openai": ("OPENAI_API_KEY", "OPENAI_MODEL"), "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")}
from .sources import discover
from .tracker import STATES, STATUSES, Tracker
from .workbook import export_workbook


def single_line(value: str) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in value).split())


def warning(message: str):
    print(f"Warning: {single_line(message)}", file=sys.stderr)


def reuse_stored_fit(stored, *, ai_requested: bool, rescore: bool, retry_fallbacks: bool = False,
                      rescore_min_score: float | None = None) -> bool:
    # Keep AI results (and failed-AI fallbacks) so reruns don't spend model calls again.
    # A plain run without --ai also keeps them, instead of overwriting them with local scores.
    # --retry-fallbacks sends only the keyword-fallback postings back to the AI.
    if not stored:
        return False
    method = stored.get("method")
    if not rescore:
        if method == "local_fallback":
            return not retry_fallbacks
        return method == "ai"
    # --rescore-ai with --rescore-min-score: keep (don't re-spend a call on) an existing AI score
    # that already cleared the given bar, and still rescore everything else (fallbacks, local-only,
    # and AI scores below the bar).
    if method == "ai" and rescore_min_score is not None:
        score = stored.get("score")
        return isinstance(score, (int, float)) and score >= rescore_min_score
    return False


def run(args, tracker: Tracker) -> int:
    settings = Settings.read(Path(args.config or os.getenv("JOB_AGENT_CONFIG", "settings.live.json")))
    profile_path = args.resume or os.getenv("JOB_AGENT_RESUME") or settings.profile
    profile = Path(profile_path).read_text(encoding="utf-8-sig")
    if not profile.strip():
        raise ValueError("Profile text is empty")
    api_key, model = None, None
    provider = args.ai_provider or "openai"
    rubric = RUBRICS[settings.fit_rubric]
    if args.ai:
        if provider not in AI_ENV:
            raise ValueError("--ai-provider must be openai or anthropic")
        key_name, model_name = AI_ENV[provider]
        api_key, model = os.getenv(key_name, "").strip(), os.getenv(model_name, "").strip()
        if not api_key or not model:
            raise ValueError(f"--ai requires {key_name} and {model_name}")

    records = []
    retrieved = 0
    ai_calls = reused = 0
    skipped = 0
    skipped_titles = 0
    successful = failed = 0
    specs = [] if args.from_db else settings.sources
    # Sources are independent network calls to a handful of shared hosts (one host per board type
    # serves every company on that board), so they are fetched concurrently rather than one at a
    # time -- a serial run of ~500 sources at up to 25s each could take the better part of an hour.
    # A bounded worker count keeps this from hammering any one host with hundreds of simultaneous
    # requests; get_json's own retry/backoff (including on 429) absorbs the rate-limit responses
    # that concurrency makes more likely.
    SOURCE_WORKERS = 16

    def fetch(spec):
        try:
            return spec, discover(spec, settings.directory), None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return spec, None, exc

    with ThreadPoolExecutor(max_workers=min(SOURCE_WORKERS, len(specs)) or 1) as pool:
        # map() preserves input order, so aggregation below is deterministic even though the
        # underlying fetches complete in whatever order the network returns them.
        results = pool.map(fetch, specs) if specs else []
        for spec, outcome, exc in results:
            if exc is not None:
                label = spec.get("path") or spec.get("board") or spec.get("title") or spec.get("type", "unknown")
                warning(f"Source {label} failed: {exc}")
                failed += 1
                continue
            jobs, priority = outcome
            retrieved += len(jobs)
            skipped += sum(not job.is_live_us() for job in jobs)
            # Keep only postings whose title matches a configured role, so non-product jobs are never stored.
            title_ok = [job for job in jobs
                        if evaluate(job, settings.policy).checks["title"].state == "pass"]
            skipped_titles += len(jobs) - len(title_ok)
            records.extend((job, priority) for job in title_ok if not job.is_demo())
            successful += 1
    if not successful and not args.from_db:
        warning("No source completed.")
        return 1

    if args.from_db:
        rows, conflicts = tracker.db.execute("SELECT * FROM jobs ORDER BY id").fetchall(), []
    else:
        rows, conflicts = tracker.ingest(records)
    for conflict in conflicts:
        warning(f"Identity conflict: {conflict}. Check the input IDs and URLs.")
    counts = Counter()
    stale = 0
    not_live = 0
    for row in rows:
        job = Job.from_dict(json.loads(row["payload"]))
        if not job.is_live_us():
            # Resolve this once instead of leaving it "pending": every run (especially --from-db, which
            # scans the whole table) was re-loading and re-checking these rows from scratch forever,
            # because skipping them here never recorded a terminal state for them to short-circuit on.
            reason = ("Sample/demo posting, not a real job" if job.is_demo() else
                      "Not confirmed as a live U.S. posting (non-U.S. location, or remote-eligibility "
                      "and location data missing or unresolved)")
            eligibility = Eligibility("excluded", {"listing": Check("fail", reason)})
            state = tracker.save_evaluation(row["id"], row["revision"], eligibility, None)
            if state is None:
                warning(f"Job {row['id']} changed during scoring. Run again to evaluate it.")
                stale += 1
            else:
                counts[state] += 1
                not_live += 1
            continue
        eligibility = evaluate(job, settings.policy)
        stored = json.loads(row["fit"]) if row["fit"] else None
        if eligibility.state != "excluded" and reuse_stored_fit(
                stored, ai_requested=args.ai, rescore=args.rescore_ai,
                retry_fallbacks=args.retry_fallbacks, rescore_min_score=args.rescore_min_score):
            state = tracker.refresh_eligibility(row["id"], row["revision"], eligibility, stored)
            if state is None:
                warning(f"Job {row['id']} changed during scoring. Run again to evaluate it.")
                stale += 1
                continue
            counts[state] += 1
            reused += 1
            continue
        fit = None
        local_score = None
        if eligibility.state != "excluded":
            ai_gate = (eligibility.checks["title"].state == "pass"
                       and eligibility.checks["location"].state != "fail")
            use_ai = bool(args.ai and ai_gate and job.description.strip()
                          and (args.ai_limit is None or ai_calls < args.ai_limit))
            if use_ai:
                ai_calls += 1
            fit = score_job(job, profile, key=api_key if use_ai else None, model=model if use_ai else None,
                            rubric=rubric, provider=provider)
            fit = replace(fit, ats=estimate_ats(profile, job.description))
            if fit.method == "ai":
                local_score = local_fit(profile, f"{job.title}\n{job.description}", rubric).score
        state = tracker.save_evaluation(row["id"], row["revision"], eligibility, fit, local_score)
        if state is None:
            warning(f"Job {row['id']} changed during scoring. Run again to evaluate it.")
            stale += 1
            continue
        counts[state] += 1
        if fit and fit.warning:
            warning(f"Job {row['id']}: {fit.warning}")
        if fit and fit.method == "local_fallback":
            counts["local_fallback"] += 1

    if args.ai:
        print(f"AI calls this run: {ai_calls}. Stored AI assessments reused: {reused}.")
    print(f"Read {retrieved} records from {successful} source(s). "
          f"Evaluated {sum(counts[state] for state in STATES)} distinct jobs.")
    if skipped:
        print(f"Omitted {skipped} non-U.S., location-unverified, or demo records from the results.")
    if skipped_titles:
        print(f"Skipped {skipped_titles} postings whose titles are not configured product roles.")
    print(" | ".join(f"{counts[state]} {state}" for state in STATES if state != "pending"))
    if not_live:
        print(f"Resolved {not_live} non-U.S./demo posting(s) that were stuck pending; they won't be rechecked again.")
    if counts["local_fallback"]:
        print(f"{counts['local_fallback']} AI assessment(s) used local fallback.")
    if args.excel:
        write_workbook(args.excel, tracker)
    return 2 if failed or conflicts or stale else 0


def write_workbook(path: Path, tracker: Tracker):
    export_workbook(path, tracker.list_jobs("all", None))
    print(f"Excel workbook saved to {path}")


def show_list(args, tracker):
    rows = tracker.list_jobs(args.state, args.status, args.limit)
    if not rows:
        print("No tracked jobs match those filters.")
    for row in rows:
        job = Job.from_dict(json.loads(row["payload"]))
        fit = json.loads(row["fit"]) if row["fit"] else None
        rank = f"{fit['score']:g}/100 ({fit['method']})" if row["score"] is not None else "unscored"
        if job.description_is_excerpt:
            rank += " [excerpt]"
        source = " | Adzuna" if job.source == "adzuna:us" else ""
        print(single_line(f"#{row['id']} [{row['state']} | {row['status']}] {job.title} at {job.company} "
                          f"| {rank} | base {job.base.display()} | total {job.total.display()}{source}"))


def positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Enter a positive integer")
    return number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Discover, rank, and track product management jobs.")
    parser.add_argument("--db", help="SQLite path (default: jobs.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("run", help="Read sources, check eligibility, and score fit")
    scan.add_argument("--config", help="Settings JSON path (default: settings.live.json)")
    scan.add_argument("--resume", help="UTF-8 resume/profile text file")
    scan.add_argument("--ai", action="store_true", help="Send resume/profile and job excerpts to the AI provider for scoring")
    scan.add_argument("--ai-provider", choices=("openai", "anthropic"), help="AI provider for --ai (default: openai)")
    scan.add_argument("--ai-limit", type=positive, help="Maximum new AI calls this run; the rest use local scoring")
    scan.add_argument("--rescore-ai", action="store_true",
                      help="Call the AI again for jobs that already have an AI or fallback assessment")
    scan.add_argument("--rescore-min-score", type=float,
                      help="With --rescore-ai, keep (skip re-scoring) existing AI scores at or above this value")
    scan.add_argument("--from-db", action="store_true",
                      help="Score the jobs already in the database without reading any job board")
    scan.add_argument("--retry-fallbacks", action="store_true",
                      help="Send postings that fell back to keyword scoring back to the AI; keep other stored scores")
    scan.add_argument("--excel", type=Path, help="Refresh an Excel workbook after the scan")
    export = commands.add_parser("export", help="Write all tracked jobs to an Excel workbook")
    export.add_argument("excel", nargs="?", type=Path, default=Path("job-results.xlsx"))
    listing = commands.add_parser("list", help="List saved jobs and separate base and total pay")
    listing.add_argument("--state", choices=("all", *STATES), default="all")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--limit", type=positive, default=30)
    detail = commands.add_parser("show", help="Show a job, eligibility reasons, and fit evidence as JSON")
    detail.add_argument("id", type=positive)
    update = commands.add_parser("status", help="Manually update tracking status and notes")
    update.add_argument("id", type=positive)
    update.add_argument("status", choices=STATUSES)
    update.add_argument("--notes", help="Replace notes. Omit to keep current notes.")
    args = parser.parse_args(argv)
    try:
        load_env()
        path = Path(args.db or os.getenv("JOB_AGENT_DB", "jobs.sqlite3"))
        excel_path = getattr(args, "excel", None)
        if excel_path is not None:
            if excel_path.suffix.lower() != ".xlsx" or excel_path.resolve() == path.resolve():
                raise ValueError("Choose a separate Excel output path ending in .xlsx")
        with Tracker(path) as tracker:
            if args.command == "run":
                return run(args, tracker)
            if args.command == "list":
                show_list(args, tracker)
            elif args.command == "export":
                write_workbook(args.excel, tracker)
            elif args.command == "show":
                print(json.dumps(tracker.show(args.id), indent=2))
            elif args.command == "status":
                tracker.set_status(args.id, args.status, args.notes)
                print(f"Job #{args.id} marked {args.status}.")
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(f"Error: {single_line(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
