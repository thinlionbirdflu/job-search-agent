import io
from importlib.util import find_spec
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zipfile import ZipFile

from job_agent.config import Settings, load_env
from job_agent.filters import SearchPolicy, distance_miles, evaluate
from job_agent.models import Job, Pay, canonical_url
from job_agent.scoring import RUBRIC, RUBRICS, Fit, ai_fit, local_fit, score_job
from job_agent.sources import discover
from job_agent.tracker import Tracker


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CENTER = (37.9577, -121.2908)
POLICY = SearchPolicy(("Senior Product Manager", "Director Product Management"), CENTER)
PROFILE = "Product management for e-commerce fulfillment platforms. Experience with APIs and integrations."
POSTING = "Own the e-commerce fulfillment roadmap and launch APIs and integrations."


def example(**changes):
    job = Job("fixture", "1", "Demo Company", "Senior Product Manager",
              "https://example.com/jobs/one", POSTING, work_mode="remote", remote_ca=True,
              base=Pay(210_000, 230_000), total=Pay(250_000, 290_000))
    return replace(job, **changes)


class FilterTests(unittest.TestCase):
    def test_base_pay_boundary_and_crossing_ranges(self):
        for pay, expected in (
            (Pay(180_000, 200_000), "excluded"),
            (Pay(200_000, 250_000), "review"),
            (Pay(200_001), "eligible"),
            (Pay(maximum=240_000), "review"),
            (Pay(), "review"),
            (Pay(300_000, currency="CAD"), "review"),
            (Pay(500, period="hour"), "review"),
        ):
            with self.subTest(pay=pay):
                self.assertEqual(evaluate(example(base=pay), POLICY).state, expected)

    def test_base_and_total_remain_separate(self):
        job = example(base=Pay(175_000, 200_000))
        self.assertEqual(evaluate(job, POLICY).state, "excluded")
        self.assertEqual(evaluate(job, replace(POLICY, pay_basis="total")).state, "eligible")
        self.assertEqual(evaluate(example(total=Pay()), replace(POLICY, pay_basis="total")).state, "review")

    def test_null_pay_target_disables_compensation_filter(self):
        policy = replace(POLICY, pay_target=None)
        for pay in (Pay(), Pay(90_000, 120_000)):
            with self.subTest(pay=pay):
                result = evaluate(example(base=pay), policy)
                self.assertEqual(result.state, "eligible")
                self.assertEqual(result.checks["compensation"].reason, "Compensation filter is off")
        self.assertEqual(evaluate(example(remote_ca=None), policy).state, "review")
        self.assertEqual(evaluate(example(work_mode="onsite"), policy).state, "excluded")

    def test_work_modes_and_california(self):
        for fields, expected in (
            ({"remote_ca": None}, "review"),
            ({"remote_ca": False}, "excluded"),
            ({"work_mode": "onsite"}, "excluded"),
            ({"work_mode": "unknown"}, "review"),
            ({"work_mode": "hybrid"}, "review"),
            ({"work_mode": "hybrid", "office_coordinates": (37.7397, -121.4252)}, "eligible"),
            ({"work_mode": "hybrid", "office_coordinates": (37.7749, -122.4194)}, "excluded"),
        ):
            with self.subTest(fields=fields):
                self.assertEqual(evaluate(example(**fields), POLICY).state, expected)

    def test_hybrid_exact_boundary_is_inclusive(self):
        office = (37.7397, -121.4252)
        job = example(work_mode="hybrid", office_coordinates=office)
        miles = distance_miles(CENTER, office)
        self.assertEqual(evaluate(job, replace(POLICY, radius_miles=miles)).state, "eligible")
        self.assertEqual(evaluate(job, replace(POLICY, radius_miles=miles - .01)).state, "excluded")

    def test_title_suffixes_and_false_positives(self):
        for title in ("Senior Product Manager, Integrations", "Director, Product Management - Fulfillment"):
            self.assertEqual(evaluate(example(title=title), POLICY).state, "eligible")
        for title in ("Associate Senior Product Manager", "Director of Product Marketing"):
            self.assertEqual(evaluate(example(title=title, base=Pay()), POLICY).state, "excluded")

    def test_invalid_inputs_are_rejected(self):
        for invalid in ({"remote_ca": "true"}, {"office_coordinates": [91, 0]}, {"url": "file:///tmp/x"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                example(**invalid)
        with self.assertRaises(ValueError):
            Pay(210_000, 209_000)

    def test_live_us_location_requires_evidence_and_filters_demo_sources(self):
        for fields, expected in (
            ({"remote_ca": None, "location": "Remote, U.S."}, True),
            ({"remote_ca": None, "location": "Sacramento, CA"}, True),
            ({"remote_ca": None, "location": "Remote"}, False),
            ({"remote_ca": None, "location": "Remote outside the U.S."}, False),
            ({"remote_ca": None, "location": "Toronto, Canada"}, False),
            ({"remote_ca": None, "location": "Tijuana, Baja California"}, False),
            ({"us_location": False}, False),
            ({"source": "sample"}, False),
            ({"source": "sample-mirror"}, False),
        ):
            with self.subTest(fields=fields):
                self.assertEqual(example(**fields).is_live_us(), expected)


class ScoringTests(unittest.TestCase):
    def test_general_rubric_scores_roles_without_commerce_terms(self):
        text = ("Owned product strategy and roadmap. Used customer feedback and KPIs. "
                "Partnered with cross-functional stakeholders and mentored team members.")
        rubric = RUBRICS["general_product"]
        fit = local_fit(text, text, rubric)
        self.assertEqual(fit.score, 85)
        self.assertEqual(fit.dimensions["strategy"].level, "strong")
        self.assertNotIn("commerce", fit.dimensions)

    def test_local_rubric_and_evidence(self):
        fit = local_fit(PROFILE, POSTING)
        self.assertEqual(fit.method, "local")
        self.assertEqual(fit.score, 67.5)
        self.assertEqual(fit.dimensions["leadership"].level, "none")
        self.assertIn(fit.dimensions["commerce"].profile_quote, PROFILE)
        self.assertIn(fit.dimensions["technical"].job_quote, POSTING)
        self.assertIsNone(score_job(example(description=""), PROFILE).score)

    def test_ai_uses_responses_schema_and_computes_score_in_python(self):
        evidence = {key: dict(level="none", profile_quote="", job_quote="", note="Missing evidence")
                    for key in RUBRIC}
        evidence["commerce"] = {
            "level": "strong", "profile_quote": "e-commerce fulfillment platforms",
            "job_quote": "e-commerce fulfillment roadmap", "note": "Direct domain alignment",
        }
        response = {"status": "completed", "model": "mock-version", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(evidence)}]}
        ]}
        with patch("job_agent.sources.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
            fit = ai_fit(PROFILE, POSTING, "mock-key", "mock-model")
        self.assertEqual((fit.method, fit.model, fit.score), ("ai", "mock-version", 30))
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertNotIn("tools", payload)

    def test_invented_quote_triggers_local_fallback(self):
        evidence = {key: dict(level="strong", profile_quote="invented", job_quote="Own",
                              note="Unsupported") for key in RUBRIC}
        response = {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(evidence)}]}
        ]}
        with patch("job_agent.scoring.get_json", return_value=response):
            fit = score_job(example(), PROFILE, key="mock-key", model="mock-model")
        self.assertEqual(fit.method, "local_fallback")
        self.assertEqual(fit.score, local_fit(PROFILE, POSTING).score)

    def test_ai_http_error_keeps_local_evidence(self):
        error = HTTPError("https://api.openai.com/v1/responses", 429, "rate limit", {}, None)
        with patch("job_agent.scoring.get_json", side_effect=error):
            fit = score_job(example(), PROFILE, key="mock-key", model="mock-model")
        self.assertEqual(fit.method, "local_fallback")
        self.assertIn("HTTP 429", fit.warning)


class DiscoveryTests(unittest.TestCase):
    def test_ashby_uses_public_board_and_checks_secondary_locations(self):
        row = {
            "jobUrl": "https://jobs.ashbyhq.com/demo/123",
            "title": "Senior Product Manager", "location": "Toronto",
            "address": {"postalAddress": {"addressCountry": "Canada"}},
            "secondaryLocations": [{"location": "Remote US", "address": {"addressCountry": "USA"}}],
            "workplaceType": "Remote", "descriptionPlain": "Build APIs",
        }
        payload = {"jobs": [row, dict(row, isListed=False),
                            dict(row, jobUrl="https://jobs.ashbyhq.com/demo/456", secondaryLocations=[])]}
        with patch("job_agent.sources.get_json", return_value=payload) as request:
            jobs, priority = discover({"type": "ashby", "board": "demo", "company": "Demo"}, ROOT)
        request.assert_called_once_with("https://api.ashbyhq.com/posting-api/job-board/demo")
        self.assertEqual(priority, 10)
        self.assertEqual([job.is_live_us() for job in jobs], [True, False])
        self.assertEqual(jobs[0].work_mode, "remote")
        self.assertIsNone(jobs[0].remote_ca)

    def test_lever_uses_public_board_and_structured_country(self):
        row = {
            "id": "one", "text": "Senior Product Manager", "hostedUrl": "https://jobs.lever.co/demo/one",
            "country": "US", "categories": {"location": "Remote"}, "workplaceType": "hybrid",
            "descriptionPlain": "Build APIs", "lists": [{"content": "<li>Own the roadmap</li>"}],
        }
        payload = [row, dict(row, id="two", country="CA", hostedUrl="https://jobs.lever.co/demo/two")]
        with patch("job_agent.sources.get_json", return_value=payload) as request:
            jobs, priority = discover({"type": "lever", "board": "demo", "company": "Demo"}, ROOT)
        request.assert_called_once_with("https://api.lever.co/v0/postings/demo?mode=json&skip=0&limit=100")
        self.assertEqual(priority, 10)
        self.assertEqual([job.is_live_us() for job in jobs], [True, False])
        self.assertEqual(jobs[0].work_mode, "hybrid")
        self.assertIn("Own the roadmap", jobs[0].description)

    def test_adzuna_search_paginates_across_employers_without_assuming_salary(self):
        record = {
            "id": 123, "title": "Senior Product Manager", "description": "Build software products",
            "company": {"display_name": "First Employer"},
            "redirect_url": "https://www.adzuna.com/jobs/details/123",
            "location": {"display_name": "Stockton, CA"},
            "salary_min": 230_000, "salary_max": 290_000, "salary_is_predicted": 1,
            "latitude": 37.95, "longitude": -121.29,
        }
        second = dict(record, id=456, redirect_url="https://www.adzuna.com/jobs/details/456",
                      company={"display_name": "Second Employer"}, salary_is_predicted=0)
        pages = [{"results": [row], "count": 2} for row in (record, second)]
        spec = {"type": "adzuna", "title": "Senior Product Manager", "where": "Stockton, CA",
                "distance_km": 81, "pages": 3, "results_per_page": 1}
        with patch.dict(os.environ, {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"}):
            with patch("job_agent.sources.get_json", side_effect=pages) as request:
                jobs, priority = discover(spec, ROOT)
        self.assertEqual(priority, 5)
        self.assertEqual([job.company for job in jobs], ["First Employer", "Second Employer"])
        self.assertEqual(request.call_count, 2)
        url = urlsplit(request.call_args.args[0])
        self.assertEqual(url.path, "/v1/api/jobs/us/search/2")
        self.assertEqual(parse_qs(url.query), {
            "app_id": ["test-id"], "app_key": ["test-key"], "title_only": ["Senior Product Manager"],
            "results_per_page": ["1"], "where": ["Stockton, CA"], "distance": ["81"],
        })
        for job in jobs:
            self.assertTrue(job.description_is_excerpt)
            self.assertIsNone(job.base.minimum)
            self.assertIsNone(job.total.maximum)
            self.assertIsNone(job.office_coordinates)
            self.assertEqual(evaluate(job, POLICY).state, "review")

    def test_adzuna_credentials_are_required_and_errors_hide_the_request_url(self):
        spec = {"type": "adzuna", "title": "Senior Product Manager"}
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Set ADZUNA_APP_ID and ADZUNA_APP_KEY"):
                discover(spec, ROOT)
        with patch.dict(os.environ, {"ADZUNA_APP_ID": "test-id", "ADZUNA_APP_KEY": "test-key"}):
            error = HTTPError("https://example.com/?app_key=test-key", 410, "authorization", {}, None)
            with patch("job_agent.sources.get_json", side_effect=error):
                with self.assertRaisesRegex(ValueError, "^Adzuna request failed with HTTP 410$"):
                    discover(spec, ROOT)

    def test_adzuna_settings_search_remote_and_local_without_employer_filters(self):
        settings = Settings.read(ROOT / "settings.adzuna.json")
        self.assertEqual(settings.fit_rubric, "general_product")
        self.assertEqual(len(settings.sources), 6)
        self.assertEqual(sum(spec.get("what") == "remote" for spec in settings.sources), 3)
        self.assertEqual(sum(spec.get("distance_km") == 81 for spec in settings.sources), 3)
        self.assertTrue(all("company" not in spec for spec in settings.sources))

    def test_greenhouse_public_read_and_unknowns(self):
        fixture = {"jobs": [{
            "id": 123, "internal_job_id": 456, "title": "Senior Product Manager",
            "absolute_url": "https://job-boards.greenhouse.io/test/jobs/123",
            "location": {"name": "Remote - US"},
            "content": "&lt;p&gt;Build APIs &amp;amp; integrations.&lt;/p&gt;&lt;script&gt;bad&lt;/script&gt;",
        }, {"id": 999, "internal_job_id": None}]}
        with patch("job_agent.sources.urlopen", return_value=io.BytesIO(json.dumps(fixture).encode())) as send:
            jobs, priority = discover({"type": "greenhouse", "board": "test", "company": "Example"}, ROOT)
        self.assertEqual((len(jobs), priority), (1, 10))
        job = jobs[0]
        self.assertEqual((job.source, job.source_id, job.work_mode), ("greenhouse:test", "123", "remote"))
        self.assertIsNone(job.remote_ca)
        self.assertIsNone(job.base.minimum)
        self.assertEqual(job.description, "Build APIs & integrations.")
        self.assertEqual(evaluate(job, POLICY).state, "review")
        request = send.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://boards-api.greenhouse.io/v1/boards/test/jobs?content=true")
        self.assertFalse(request.has_header("Authorization"))

    def test_canonical_urls_keep_job_identity(self):
        left = canonical_url("https://BOARDS.greenhouse.io/demo/jobs/42?utm_source=feed#apply")
        right = canonical_url("https://job-boards.greenhouse.io/demo/jobs/42/")
        self.assertEqual(left, right)
        self.assertNotEqual(canonical_url("https://example.com/jobs?gh_jid=1"),
                            canonical_url("https://example.com/jobs?gh_jid=2"))
        self.assertNotEqual(canonical_url("https://example.com/jobs#/job/1"),
                            canonical_url("https://example.com/jobs#/job/2"))


class TrackerTests(unittest.TestCase):
    def test_existing_demo_and_non_us_jobs_do_not_appear_in_results(self):
        with Tracker(Path(":memory:")) as tracker:
            live = example(source="live", source_id="one")
            demo = replace(live, source="sample", url="https://demo.example/jobs/one")
            abroad = replace(live, source_id="two", url="https://example.com/jobs/two",
                             remote_ca=None, location="London, United Kingdom")
            tracker.ingest([(job, 10) for job in (live, demo, abroad)])
            rows = tracker.list_jobs("all", None)
            self.assertEqual([row["id"] for row in rows], [1])
            tracker.ingest([(replace(live, remote_ca=None, us_location=False), 10)])
            self.assertEqual(tracker.list_jobs("all", None), [])
            self.assertEqual(tracker.show(1)["job"]["us_location"], False)
    def test_fit_threshold_preserves_hard_filters_and_review(self):
        with Tracker(Path(":memory:")) as tracker:
            row = tracker.ingest([(example(), 10)])[0][0]
            for job, score, expected in (
                (example(), 59.99, "rejected"),
                (example(), 60, "eligible"),
                (example(remote_ca=None), 59, "rejected"),
                (example(remote_ca=None), 60, "review"),
                (example(), None, "review"),
                (example(work_mode="onsite"), None, "excluded"),
            ):
                with self.subTest(expected=expected, score=score):
                    state = tracker.save_evaluation(row["id"], row["revision"], evaluate(job, POLICY),
                                                    Fit("local", score, {}))
                    self.assertEqual(state, expected)
                    self.assertEqual(tracker.show(row["id"])["state"], expected)

    def test_legacy_classifications_migrate_without_losing_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with Tracker(path) as tracker:
                records = [(example(source_id=str(i), url=f"https://example.com/{i}"), 10) for i in range(3)]
                rows, _ = tracker.ingest(records)
                legacy = (("rejected", None), ("review", 55), ("eligible", None))
                with tracker.db:
                    tracker.db.executemany("UPDATE jobs SET state = ?, score = ? WHERE id = ?",
                                           [(*state, row["id"]) for state, row in zip(legacy, rows)])
                    tracker.db.execute("PRAGMA user_version = 0")
                tracker.set_status(2, "saved", "Follow up")
            with Tracker(path) as tracker:
                self.assertEqual(tracker.show(1)["state"], "excluded")
                job = tracker.show(2)
                self.assertEqual((job["state"], job["status"], job["notes"]), ("rejected", "saved", "Follow up"))
                self.assertEqual(tracker.show(3)["state"], "review")
            with Tracker(path) as tracker:
                self.assertEqual(tracker.show(2)["state"], "rejected")

    def test_identity_persists_across_scans_and_prefers_manual_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite3"
            original = example(base=Pay())
            manual = example(source="manual", source_id="M1", url=original.url + "?utm_source=copy")
            with Tracker(path) as tracker:
                rows, conflicts = tracker.ingest([(original, 10), (manual, 100)])
                self.assertFalse(conflicts)
                self.assertEqual(len(rows), 1)
                job_id = rows[0]["id"]
                tracker.set_status(job_id, "interviewing", "Talk to hiring manager")
            with Tracker(path) as tracker:
                changed_url = replace(original, url="https://example.com/jobs/renamed")
                rows, conflicts = tracker.ingest([(changed_url, 10)])
                self.assertFalse(conflicts)
                self.assertEqual(rows[0]["id"], job_id)
                result = tracker.show(job_id)
                self.assertEqual(result["status"], "interviewing")
                self.assertEqual(result["notes"], "Talk to hiring manager")
                self.assertEqual(result["job"]["base"]["minimum"], 210_000)
                self.assertEqual(len(result["identities"]), 4)

    def test_stale_evaluation_and_conflicting_identity(self):
        with Tracker(Path(":memory:")) as tracker:
            original, other = example(), example(source_id="2", url="https://example.com/jobs/two")
            rows, _ = tracker.ingest([(original, 10), (other, 10)])
            first = rows[0]
            _, conflicts = tracker.ingest([(replace(original, url=other.url), 10)])
            self.assertEqual(len(conflicts), 1)
            tracker.ingest([(replace(original, description="New description"), 10)])
            self.assertFalse(tracker.save_evaluation(first["id"], first["revision"], evaluate(original, POLICY), None))
            self.assertEqual(tracker.show(first["id"])["state"], "pending")


class CLITests(unittest.TestCase):
    def cli(self, db, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run([sys.executable, "-m", "job_agent", "--db", str(db), *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")

    def test_demo_run_list_show_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "demo.sqlite3"
            result = self.cli(db, "run", "--config", str(FIXTURES / "settings.json"),
                              "--resume", str(FIXTURES / "profile.txt"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Read 8 records", result.stdout)
            self.assertIn("2 eligible | 0 review | 2 rejected | 3 excluded", result.stdout)
            result = self.cli(db, "list", "--state", "eligible")
            self.assertIn("base USD", result.stdout)
            self.assertIn("total USD", result.stdout)
            self.assertNotIn("[review", result.stdout)
            self.assertEqual(self.cli(db, "status", "1", "applied", "--notes", "Manually recorded").returncode, 0)
            self.assertEqual(self.cli(db, "run", "--config", str(FIXTURES / "settings.json"),
                                      "--resume", str(FIXTURES / "profile.txt")).returncode, 0)
            report = json.loads(self.cli(db, "show", "1").stdout)
            self.assertEqual(report["status"], "applied")
            self.assertEqual(report["notes"], "Manually recorded")
            self.assertEqual(report["fit"]["method"], "local")
            self.assertEqual(len(report["identities"]), 3)

    def test_partial_run_reports_source_failure_and_preserves_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db, config, jobs = (root / name for name in ("demo.sqlite3", "settings.json", "jobs.json"))
            jobs.write_text(json.dumps([asdict(example())]), encoding="utf-8")
            config.write_text(json.dumps({
                "filters": asdict(POLICY), "profile": str(FIXTURES / "profile.txt"),
                "sources": [{"type": "json", "path": "missing.json"}, {"type": "json", "path": "jobs.json"}],
            }), encoding="utf-8")
            result = self.cli(db, "run", "--config", str(config), "--resume", str(FIXTURES / "profile.txt"))
            self.assertEqual(result.returncode, 2)
            self.assertIn("missing.json failed", result.stderr)
            self.assertIn("1 eligible", result.stdout)

    def test_environment_file_respects_shell_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('# Local settings\nJOB_AGENT_DB="local tracker.sqlite3"\n'
                            'JOB_AGENT_RESUME=local.txt\n', encoding="utf-8")
            os.environ["JOB_AGENT_RESUME"] = "shell.txt"
            load_env(path)
            self.assertEqual(os.environ["JOB_AGENT_DB"], "local tracker.sqlite3")
            self.assertEqual(os.environ["JOB_AGENT_RESUME"], "shell.txt")

    @unittest.skipUnless(find_spec("xlsxwriter"), "install requirements-excel.txt for Excel export tests")
    def test_export_creates_filterable_workbook_with_scores_links_and_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "demo.sqlite3"
            excel = Path(directory) / "results.xlsx"
            result = self.cli(db, "run", "--config", str(FIXTURES / "settings.json"),
                              "--resume", str(FIXTURES / "profile.txt"), "--excel", str(excel))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.cli(db, "status", "1", "saved", "--notes", "=2+2").returncode, 0)
            result = self.cli(db, "export", str(excel))
            self.assertEqual(result.returncode, 0, result.stderr)
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            with ZipFile(excel) as workbook:
                names = ElementTree.fromstring(workbook.read("xl/workbook.xml")).findall(".//x:sheet", ns)
                self.assertEqual([node.get("name") for node in names], ["Job results", "Excluded"])
                sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
                excluded = ElementTree.fromstring(workbook.read("xl/worksheets/sheet2.xml"))
                self.assertEqual(sheet.find("x:autoFilter", ns).get("ref"), "A4:O8")
                self.assertEqual(excluded.find("x:autoFilter", ns).get("ref"), "A4:O7")
                self.assertIsNotNone(sheet.find(".//x:pane", ns))
                self.assertEqual(len(sheet.findall(".//x:hyperlink", ns)), 4)
                self.assertEqual(sheet.find(".//x:c[@r='E5']/x:v", ns).text, "67.5")
                self.assertEqual(sheet.find(".//x:c[@r='M5']", ns).get("t"), "s")
                self.assertIn(b"=2+2", workbook.read("xl/sharedStrings.xml"))


if __name__ == "__main__":
    unittest.main()
