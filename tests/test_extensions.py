import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from job_agent.config import Settings
from job_agent.filters import JUNIOR_TITLE, SearchPolicy, evaluate, title_forms
from job_agent.models import Job, Pay, infer_from_description, infer_remote_ca, infer_work_mode
from job_agent.sources import discover
from job_agent.scoring import ANTHROPIC_URL, RUBRIC, anthropic_fit, score_job
from job_agent.__main__ import main, reuse_stored_fit
from job_agent.tracker import Tracker, decide_state
from job_agent.filters import Eligibility, Check

ROOT = Path(__file__).resolve().parents[1]
CENTER = (37.9577, -121.2908)
POLICY = SearchPolicy(("Senior Product Manager", "Sr Product Manager", "Head of Product Management"), CENTER)


def job(**changes):
    base = Job("fixture", "1", "Demo Co", "Senior Product Manager", "https://example.com/jobs/1",
               "Own the roadmap.", work_mode="unknown", remote_ca=None, base=Pay(210_000, 230_000))
    for key, value in changes.items():
        base = Job(**{**base.__dict__, key: value})
    return base


class TextInferenceTests(unittest.TestCase):
    def test_remote_california_eligibility_on_one_line(self):
        self.assertIs(infer_remote_ca("Remote role. Candidates must reside in California."), True)
        self.assertIs(infer_remote_ca("This is a remote position open to candidates anywhere in the US."), True)

    def test_explicit_california_exclusion_is_definite(self):
        self.assertIs(infer_remote_ca("Remote, not available in California."), False)
        self.assertIs(infer_remote_ca("Remote role; candidates must be based outside California."), False)

    def test_company_boilerplate_and_split_evidence_stay_unknown(self):
        self.assertIsNone(infer_remote_ca("Our headquarters is in California and we value remote-friendly culture."))
        self.assertIsNone(infer_remote_ca("Remote role.\nEligible candidates must reside in California."))
        self.assertIsNone(infer_remote_ca("Build APIs and integrations."))

    def test_work_mode_phrases(self):
        self.assertEqual(infer_work_mode("This is a hybrid role, 3 days a week in office."), "hybrid")
        self.assertEqual(infer_work_mode("This role is onsite in our Stockton office."), "onsite")
        self.assertEqual(infer_work_mode("Our hybrid culture rewards curiosity."), "unknown")

    def test_inference_fills_only_unknown_fields(self):
        enriched = infer_from_description(job(description="Remote role. Candidates must reside in California."))
        self.assertEqual((enriched.work_mode, enriched.remote_ca), ("remote", True))
        self.assertEqual(evaluate(enriched, POLICY).state, "eligible")
        verified = job(description="Remote role. Candidates must reside in California.", work_mode="hybrid", remote_ca=False)
        self.assertIs(infer_from_description(verified), verified)

    def test_contradictory_hybrid_and_remote_leave_remote_unresolved(self):
        enriched = infer_from_description(job(description="Hybrid role, 2 days a week in office.\n"
                                                          "Remote role. Candidates must reside in California."))
        self.assertEqual(enriched.work_mode, "hybrid")
        self.assertIsNone(enriched.remote_ca)


class TitleTests(unittest.TestCase):
    def test_abbreviations_match_configured_titles(self):
        self.assertIn("senior product manager", title_forms("Sr. Product Manager"))
        self.assertEqual(evaluate(job(title="Sr. Product Manager, Payments", remote_ca=True, work_mode="remote"),
                                  POLICY).state, "eligible")
        self.assertEqual(evaluate(job(title="Senior PM, Platform", remote_ca=True, work_mode="remote"),
                                  POLICY).state, "eligible")

    def test_head_of_product_management_is_now_a_configured_role(self):
        self.assertEqual(evaluate(job(title="Head of Product Management", remote_ca=True, work_mode="remote"),
                                  POLICY).state, "eligible")

    def test_junior_titles_are_rejected_at_the_title_gate(self):
        for title in ("Associate Product Manager", "Junior Product Manager", "Product Management Intern"):
            with self.subTest(title=title):
                self.assertTrue(JUNIOR_TITLE.search(title.casefold()))
                check = evaluate(job(title=title, remote_ca=True, work_mode="remote"),
                                 SearchPolicy(("Product Manager",), CENTER)).checks["title"]
                self.assertEqual(check.state, "fail")

    def test_associate_director_is_not_treated_as_junior(self):
        self.assertFalse(JUNIOR_TITLE.search("associate director of product"))


class DiscoveryTests(unittest.TestCase):
    def test_ashby_discovered_text_is_inferred(self):
        row = {"jobUrl": "https://jobs.ashbyhq.com/demo/9", "title": "Senior Product Manager",
               "location": "Remote", "workplaceType": "Remote",
               "descriptionPlain": "Remote role. Candidates must reside in California."}
        with patch("job_agent.sources.get_json", return_value={"jobs": [row]}):
            jobs, _ = discover({"type": "ashby", "board": "demo", "company": "Demo"}, ROOT)
        self.assertIs(jobs[0].remote_ca, True)

    def test_json_records_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            path.write_text(json.dumps([{
                "source": "manual", "source_id": "1", "company": "Demo", "title": "Senior Product Manager",
                "url": "https://example.com/1", "description": "Remote role. Candidates must reside in California.",
            }]), encoding="utf-8")
            jobs, _ = discover({"type": "json", "path": "jobs.json"}, Path(directory))
        self.assertIsNone(jobs[0].remote_ca)

    def test_recruitee_offers_map_to_normalized_jobs(self):
        payload = {"offers": [{
            "id": 42, "title": "Senior Product Manager", "careers_url": "https://demo.recruitee.com/o/pm",
            "description": "<p>Own the roadmap.</p>", "requirements": "<ul><li>APIs</li></ul>",
            "city": "Austin", "state_name": "Texas", "country": "United States", "country_code": "US",
            "remote": True, "hybrid": False,
        }]}
        with patch("job_agent.sources.get_json", return_value=payload) as request:
            jobs, priority = discover({"type": "recruitee", "slug": "demo", "company": "Demo"}, ROOT)
        request.assert_called_once_with("https://demo.recruitee.com/api/offers/")
        self.assertEqual(priority, 10)
        self.assertEqual((jobs[0].source, jobs[0].source_id, jobs[0].work_mode, jobs[0].us_location),
                         ("recruitee:demo", "42", "remote", True))
        self.assertIn("Own the roadmap.", jobs[0].description)
        self.assertIn("APIs", jobs[0].description)

    def test_recruitee_rejects_bad_slugs(self):
        with self.assertRaises(ValueError):
            discover({"type": "recruitee", "slug": "../evil", "company": "Demo"}, ROOT)

    def test_smartrecruiters_pages_and_fetches_details(self):
        summary = {"id": "7001", "name": "Senior Product Manager", "location": {
            "city": "Remote", "country": "us", "remote": True, "fullLocation": "Remote, US"}}
        detail = {"postingUrl": "https://jobs.smartrecruiters.com/demo/7001", "jobAd": {"sections": {
            "jobDescription": {"title": "Job", "text": "<p>Own the roadmap.</p>"},
            "qualifications": {"title": "Quals", "text": "<p>Platform APIs</p>"}}}}

        def fake_get(url):
            if url.endswith("/postings/7001"):
                return detail
            self.assertIn("offset=0", url)
            return {"content": [summary], "totalFound": 1}

        with patch("job_agent.sources.get_json", side_effect=fake_get) as request:
            jobs, _ = discover({"type": "smartrecruiters", "company_id": "demo", "company": "Demo"}, ROOT)
        self.assertEqual(request.call_count, 2)  # one list page, one detail fetch
        self.assertEqual((jobs[0].source, jobs[0].work_mode, jobs[0].us_location), ("smartrecruiters:demo", "remote", True))
        self.assertEqual(jobs[0].url, "https://jobs.smartrecruiters.com/demo/7001")
        self.assertIn("Platform APIs", jobs[0].description)


PROFILE_TEXT = "Product management for e-commerce fulfillment platforms. Experience with APIs and integrations."
POSTING_TEXT = "Own the e-commerce fulfillment roadmap and launch APIs and integrations."


def anthropic_response(rating):
    return {"model": "claude-haiku-5-5", "stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "record_job_fit", "input": rating}]}


GOOD_RATING = {
    "commerce": {"level": "strong", "profile_quote": "e-commerce fulfillment platforms",
                 "job_quote": "e-commerce fulfillment roadmap", "note": "Shared fulfillment focus."},
    "technical": {"level": "strong", "profile_quote": "APIs and integrations",
                  "job_quote": "APIs and integrations", "note": "Shared API work."},
    "product": {"level": "none", "profile_quote": "", "job_quote": "", "note": "Not stated in the posting."},
    "leadership": {"level": "none", "profile_quote": "", "job_quote": "", "note": "No people scope stated."},
}


class AnthropicScoringTests(unittest.TestCase):
    def test_request_uses_forced_tool_and_anthropic_headers(self):
        with patch("job_agent.scoring.get_json", return_value=anthropic_response(GOOD_RATING)) as request:
            fit = anthropic_fit(PROFILE_TEXT, POSTING_TEXT, "test-key", "claude-haiku-5-5", RUBRIC)
        self.assertEqual(request.call_args.args[0], ANTHROPIC_URL)
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "record_job_fit"})
        self.assertEqual(payload["model"], "claude-haiku-5-5")
        self.assertEqual(payload["tools"][0]["input_schema"]["required"], list(RUBRIC))
        headers = request.call_args.kwargs["extra_headers"]
        self.assertEqual(headers, {"x-api-key": "test-key", "anthropic-version": "2023-06-01"})
        self.assertNotIn("api_key", request.call_args.kwargs)
        self.assertEqual((fit.method, fit.model, fit.score), ("ai", "claude-haiku-5-5", 55))

    def test_unverified_quote_falls_back_to_local_scoring(self):
        bad = dict(GOOD_RATING, commerce=dict(GOOD_RATING["commerce"], job_quote="invented phrase"))
        job = Job("fixture", "1", "Demo Co", "Senior Product Manager", "https://example.com/jobs/1", POSTING_TEXT)
        with patch("job_agent.scoring.get_json", return_value=anthropic_response(bad)):
            fit = score_job(job, PROFILE_TEXT, key="test-key", model="claude-haiku-5-5", provider="anthropic")
        self.assertEqual(fit.method, "local_fallback")
        self.assertTrue(fit.warning.startswith("AI assessment unavailable"))

    def test_missing_tool_call_is_rejected(self):
        response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Sure."}]}
        with patch("job_agent.scoring.get_json", return_value=response):
            with self.assertRaises(ValueError):
                anthropic_fit(PROFILE_TEXT, POSTING_TEXT, "test-key", "claude-haiku-5-5", RUBRIC)

    def test_unknown_provider_is_rejected(self):
        job = Job("fixture", "1", "Demo Co", "Senior Product Manager", "https://example.com/jobs/1", POSTING_TEXT)
        with self.assertRaises(ValueError):
            score_job(job, PROFILE_TEXT, key="k", model="m", provider="gemini")

    def test_cli_requires_anthropic_credentials_before_network_access(self):
        with patch("job_agent.__main__.load_env"), patch.dict(os.environ, {}, clear=True), \
                patch("job_agent.__main__.discover") as discovery, tempfile.TemporaryDirectory() as directory:
            code = main(["--db", str(Path(directory) / "t.sqlite3"), "run",
                         "--config", str(ROOT / "settings.live.json"), "--ai", "--ai-provider", "anthropic"])
        self.assertEqual(code, 1)
        discovery.assert_not_called()


class ReuseTests(unittest.TestCase):
    def test_decide_state_thresholds_and_ats_override(self):
        self.assertEqual(decide_state("eligible", 70, None), "eligible")
        self.assertEqual(decide_state("eligible", 55, None), "rejected")
        self.assertEqual(decide_state("eligible", 55, 85), "eligible")
        self.assertEqual(decide_state("review", None, None), "review")
        self.assertEqual(decide_state("excluded", 90, 95), "excluded")

    def test_stored_ai_and_fallback_results_are_reused_unless_forced(self):
        for method in ("ai", "local_fallback"):
            self.assertTrue(reuse_stored_fit({"method": method}, ai_requested=True, rescore=False))
            self.assertTrue(reuse_stored_fit({"method": method}, ai_requested=False, rescore=False))
            self.assertFalse(reuse_stored_fit({"method": method}, ai_requested=True, rescore=True))
        self.assertFalse(reuse_stored_fit({"method": "local"}, ai_requested=True, rescore=False))
        self.assertFalse(reuse_stored_fit(None, ai_requested=True, rescore=False))

    def test_refresh_keeps_stored_score_and_recomputes_state(self):
        job = Job("fixture", "9", "Demo Co", "Senior Product Manager", "https://example.com/jobs/9", POSTING_TEXT,
                  work_mode="remote", remote_ca=True)
        with tempfile.TemporaryDirectory() as directory:
            with Tracker(Path(directory) / "t.sqlite3") as tracker:
                row = tracker.ingest([(job, 10)])[0][0]
                stored = {"method": "ai", "score": 72.0, "ats": None}
                eligibility = Eligibility("eligible", {"title": Check("pass", "ok")})
                state = tracker.refresh_eligibility(row["id"], row["revision"], eligibility, stored)
                saved = tracker.db.execute("SELECT state, score FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        self.assertEqual(state, "eligible")
        self.assertEqual(tuple(saved), ("eligible", None))

class ResilienceTests(unittest.TestCase):
    def test_discovery_retries_a_timeout_then_succeeds(self):
        import io
        from job_agent.sources import get_json
        ok = io.BytesIO(b'{"jobs": []}')
        with patch("job_agent.sources.urlopen", side_effect=[TimeoutError("The read operation timed out"), ok]), \
                patch("job_agent.sources.time.sleep"):
            self.assertEqual(get_json("https://example.com/x"), {"jobs": []})

    def test_model_calls_do_not_retry(self):
        from job_agent.sources import get_json
        with patch("job_agent.sources.urlopen", side_effect=TimeoutError("slow")) as request, \
                patch("job_agent.sources.time.sleep"):
            with self.assertRaises(TimeoutError):
                get_json("https://example.com/x", payload={"a": 1}, api_key="k")
        self.assertEqual(request.call_count, 1)

    def test_extra_model_keys_are_ignored_but_missing_ones_are_named(self):
        from job_agent.scoring import validate_evidence
        rating = {name: {"level": "none", "profile_quote": "", "job_quote": "", "note": "Not stated.", "extra": 1}
                  for name in RUBRIC}
        evidence = validate_evidence(rating, PROFILE_TEXT, POSTING_TEXT, RUBRIC)
        self.assertEqual(set(evidence), set(RUBRIC))
        rating["product"] = {"level": "none", "profile_quote": "", "job_quote": ""}
        with self.assertRaises(ValueError) as caught:
            validate_evidence(rating, PROFILE_TEXT, POSTING_TEXT, RUBRIC)
        self.assertIn("incomplete evidence fields", str(caught.exception))


class QuoteCheckTests(unittest.TestCase):
    def rating(self, job_quote):
        from job_agent.scoring import validate_evidence
        rating = {name: {"level": "none", "profile_quote": "", "job_quote": "", "note": "Not stated."}
                  for name in RUBRIC}
        rating["commerce"] = {"level": "strong", "profile_quote": "e-commerce fulfillment platforms",
                              "job_quote": job_quote, "note": "Shared focus."}
        return validate_evidence(rating, PROFILE_TEXT, POSTING_TEXT, RUBRIC)

    def test_typographic_and_spacing_differences_are_accepted(self):
        self.assertEqual(self.rating("E-commerce  fulfillment\u00a0roadmap")["commerce"].level, "strong")
        self.assertEqual(self.rating("\"Own the e-commerce fulfillment roadmap\"")["commerce"].level, "strong")

    def test_invented_quotes_are_still_rejected(self):
        with self.assertRaises(ValueError):
            self.rating("own the warehouse robotics program")


class SettingsTests(unittest.TestCase):
    def test_live_settings_include_head_of_product_management(self):
        settings = Settings.read(ROOT / "settings.live.json")
        self.assertIn("Head of Product Management", settings.policy.titles)


if __name__ == "__main__":
    unittest.main()
