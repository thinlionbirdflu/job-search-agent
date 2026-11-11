# Product management job-search agent

Discover roles, apply explicit location and compensation rules, compare listings with your experience, and keep a ranked shortlist in SQLite. The only application actions are the statuses you record manually.

## Create your settings file

Settings files hold your search configuration and stay on your machine. They are not committed to this repository, so you create one from the template:

1. Copy `settings.example.json` to `settings.local.json` in the project folder.
2. Edit `settings.local.json` to add the boards and filters you want. Keep your settings there.
3. Run the agent with `--config settings.local.json`.

The default config is `settings.live.json`. To use that name instead, copy the template to `settings.live.json`. Both names are ignored by git, so your settings never reach GitHub. Keep API keys in `.env`, not in a settings file.

## Run a live search

Use Python 3.11 or newer. Put your resume-based profile.txt in this folder, then run these commands from it.

~~~powershell
python -m job_agent --db search.sqlite3 run --config settings.live.json
python -m job_agent --db search.sqlite3 list --state eligible
python -m job_agent --db search.sqlite3 list --state review
~~~

The live settings use public employer feeds from Greenhouse, Ashby, and Lever. These feeds require no signup or API key. Older demo rows are hidden from terminal results and the Excel workbook when refreshed. Automated tests use isolated fixtures.

~~~powershell
python -m unittest discover -s tests -v
~~~

## Search settings

Copy `settings.live.json` to settings.local.json if you want to edit the live search.

~~~powershell
python -m job_agent --db search.sqlite3 run --config settings.local.json --resume resume.txt
python -m job_agent --db search.sqlite3 list --state eligible
python -m job_agent --db search.sqlite3 list --state review
~~~

| Rule | Default behavior |
| --- | --- |
| Role | Configured titles such as Senior Product Manager, Director of Product Management, and Head of Product Management. "Sr" and "PM" are read as Senior and Product Manager, and team suffixes separated by punctuation are accepted. Titles that signal a junior level (Associate, Junior, Intern, Apprentice) are excluded, while "Associate Director" is not. Edit filters.titles to add other titles. |
| Country | U.S. location evidence is required. The agent checks structured country data where available and recognizes explicit U.S. location labels. Foreign and country-unverified postings are omitted from results. |
| Remote | Must permit work from California. Use remote_ca only when the posting's rules support a definite answer. For discovered postings, a single line or bullet that says a remote role must reside in California or anywhere in the U.S. sets remote_ca to true, and one that excludes California sets it to false. Split or ambiguous wording stays in review. Values you enter in a JSON record are never overwritten. |
| Hybrid | Within 50 straight-line miles of the configured approximate Stockton center. Use office coordinates rather than city centers where available. The radius measures distance, not driving time. |
| Pay | Salary filtering is off in the live settings. To require annual USD pay strictly above $200,000, set filters.pay_target to 200000. Set filters.pay_basis to total to use disclosed total compensation. Base and total are displayed independently. |
| Pay certainty | A verified floor above the target passes. A verified ceiling at or below it fails. A crossing range, missing pay, other currency or period, or a floor at or below the target with no ceiling goes to review. A single known fixed amount can be entered as both bounds. |

Set `"pay_target": null` inside `filters` to turn off the compensation filter. Known base and total pay still appear in the tracker. Roles with missing or below-target pay can qualify when the title and location checks pass. Keep the numeric target to flag unknown pay for review and reject verified ceilings at or below the target.

Any failed title, location, or compensation check excludes the listing. The agent then scores the remaining jobs. Fit of 60 or higher or estimated ATS strictly above 80 qualifies the job when all rules pass, or sends it to review when a rule is unresolved. A fit score below 60 is rejected if estimated ATS is 80 or lower or unavailable. A potential match with no scores goes to review. Use the full report from show to see each reason. If pay varies by geography, enter only the range that applies to the office or California remote arrangement you would pursue.

## Search across employers with Adzuna

`settings.adzuna.json` searches Adzuna's U.S. listings by product management title across industries. It runs nationwide searches with the keyword remote and location searches within 81 km of Stockton, CA. The same title, California eligibility, 50-mile hybrid radius, and base pay rules still apply when the agent evaluates each result. You can edit the search titles or location in the settings file. Each source fetches up to 50 results per run by default. Set pages to fetch more per query, up to 10, within your API plan limits.

Register for an Adzuna application ID and key, then add ADZUNA_APP_ID and ADZUNA_APP_KEY to .env in the project folder. Keep them out of the settings JSON. Save your own profile.txt next to settings.adzuna.json or pass a different profile with --resume. [Adzuna API registration and setup](https://developer.adzuna.com/overview)

~~~powershell
python -m job_agent --db search.sqlite3 run --config settings.adzuna.json
python -m job_agent --db search.sqlite3 list --state review
~~~

Adzuna provides posting excerpts. The agent identifies excerpt-based scores and leaves annual base and total pay, California remote eligibility, and office coordinates unverified. Open the original listing link to confirm those details. You can import a verified JSON record using the same posting URL to update its eligibility. [Adzuna search API](https://developer.adzuna.com/docs/search)

The Adzuna settings use the general_product fit rubric. It weights product strategy and roadmap ownership at 30%, discovery, delivery, and measurement at 30%, leadership at 25%, and APIs and integrations at 15%. Any settings file can select this rubric with `"fit_rubric": "general_product"`. E-commerce is still part of your profile when it supports a match.

## Discover and import listings

The sources array can combine public Greenhouse, Ashby, and Lever employer boards, optional Adzuna queries, and JSON files.

`settings.live.json` contains 485 public board sources, including 237 Greenhouse, 177 Ashby, and 71 Lever. The boards were identified through public employer board pages and indexed job postings for target and adjacent senior product roles. `settings.greenhouse.json` is the Greenhouse-only watchlist. Postings and boards can change between runs.

~~~json
"sources": [
  {"type": "json", "path": "jobs.local.json"},
  {"type": "greenhouse", "board": "EMPLOYER_BOARD_TOKEN", "company": "Employer name"}
]
~~~

- **JSON.** Enter listings you find manually or normalize records from an authorized export. Paths are relative to the settings file. Each row requires source, source_id, company, title, and url. Use a stable source name and ID. Optional fields are description, location, us_location (true, false, or null), work_mode (remote, hybrid, onsite, or unknown), remote_ca (true, false, or null), office_coordinates ([latitude, longitude] or null), and base and total pay objects. Set us_location to true when you have verified a U.S. location. Each pay object accepts minimum, maximum, currency (default USD), and period (default year). Omit values you cannot verify. Use annual base or total figures directly where the posting supplies them.
- **Greenhouse.** Supply an actual employer board token from its public Greenhouse board URL and the company name. The adapter reads the employer's public listings and descriptions through the documented GET endpoint. It infers work mode from explicit location labels and leaves California eligibility, pay, and office coordinates for review. To enrich a listing, import its details in a JSON row with the same job URL. [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- **Ashby and Lever.** Use type ashby or lever and specify board and company. Lever also supports region eu for boards hosted on its European instance. Both adapters use published listings and structured country and workplace fields where available. The agent still checks California eligibility and hybrid distance separately. [Ashby public Job Postings API](https://developers.ashbyhq.com/docs/public-job-posting-api) · [Lever Postings API](https://github.com/lever/postings-api)
- **Recruitee and SmartRecruiters.** Use type recruitee with a slug (the company subdomain), or type smartrecruiters with a company_id. Both read public posting data and map location and remote flags into the same rules. Their adapters are covered by fixture tests only, because the public endpoints could not be reached from the environment where they were written. Confirm one board returns postings before relying on either type.
~~~json
{"type": "recruitee", "slug": "EMPLOYER_SUBDOMAIN", "company": "Employer name"}
{"type": "smartrecruiters", "company_id": "EMPLOYER_IDENTIFIER", "company": "Employer name"}
~~~
- **Adzuna.** Set type to adzuna and provide a title for each search. Optional what adds a keyword. Optional where sets the location, and distance_km sets the provider's search radius around it. The app displays Adzuna as the source. The default record priority for Adzuna is 5.

Jobs match by source plus source ID or by canonical posting URL. The tracker recognizes both Greenhouse-hosted URL forms, drops known tracking parameters, and preserves parameters that can identify a job. Distinct links and source IDs are kept separate when the agent cannot establish a match. If two existing tracker IDs are linked by a conflicting record, the run reports the conflict and preserves both records. A JSON record takes precedence over a Greenhouse record by default. An integer priority in a source config overrides its default (JSON 100, Greenhouse 10). The preferred record is stored as a whole, so include its description and other fields when supplying an override. If two records have equal priority, the later record wins.

The registry in job_agent/sources.py is the extension point for other authorized sources. Failed sources produce warnings. A run returns 1 if every source fails and 2 for a partial run or a tracking conflict. A successful source can legitimately return zero jobs. Previously tracked listings stay in the database if they disappear from a feed. Use last_seen and the original posting URL to check freshness. Re-running evaluates listings returned on that run and preserves the status and notes you entered.

## Compare listings with your resume

Save a UTF-8 plain-text resume or tailored experience summary as profile.txt or pass it with --resume. The score is an evidence-based comparison out of 100. Each dimension receives none, partial, or strong credit (0%, 50%, or 100% of its weight). The live settings use general_product. The original commerce rubric remains available for searches focused on fulfillment.

| Dimension | Weight |
| --- | ---: |
| E-commerce and fulfillment | 30 |
| Platform APIs and integrations | 25 |
| Product management execution | 25 |
| Leadership and scope | 20 |

Local mode looks for two groups of related terms per dimension in both texts and shows the supporting excerpts. A shared term is a signal that merits a closer read. The score measures overlap with this profile and rubric. Use show to inspect the evidence and gaps, particularly around people management and the scope of director roles. Listings without a description remain unscored. The list puts eligible roles first and orders by score within each eligibility group.

### Optional AI scoring with Anthropic

Create an API key in the Claude Console (console.anthropic.com), add billing, and put these lines in .env:

~~~
ANTHROPIC_API_KEY=your-key
ANTHROPIC_MODEL=claude-haiku-5-5
~~~

Then run with the Anthropic provider:

~~~powershell
python -m job_agent --db search.sqlite3 run --config settings.local.json --resume resume.txt --ai --ai-provider anthropic
~~~

The Anthropic scorer uses forced tool use so the rating comes back as structured data. The same quote checks, eligibility rules, and local fallback apply. API calls are billed to your Console balance. The model name is any Claude model enabled for your key.

### Optional OpenAI scoring

Copy .env.example to .env, set OPENAI_API_KEY and OPENAI_MODEL, then opt in with the following command.

~~~powershell
python -m job_agent --db search.sqlite3 run --config settings.local.json --resume resume.txt --ai
~~~

Use a model enabled for your API project that supports Structured Outputs. The AI rates each dimension, cites both texts for positive ratings, and explains any gaps. Python verifies that those quotes appear in the supplied excerpts and calculates the weighted score. The model cannot change eligibility rules or update application status. It may interpret evidence differently from the local scorer, so the report identifies its scoring method. If a request fails or the evidence is invalid, the report labels the result as local_fallback. [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)

With --ai, each eligible or review listing with a description sends up to 16,000 characters of the profile and 20,000 characters of the listing to the selected AI provider. The request sets store to false. API usage may incur charges. The SQLite file can contain excerpts from your resume and any notes you add. Remove contact details or other information you prefer to keep private before scoring. The tests exercise both integrations with simulated responses.

## Track a job

Run list to get an ID, then run show ID to see the posting URL, the separate pay figures, each rule's decision, evidence, first and last seen timestamps, and notes. Use status ID STATUS with optional --notes to record progress. Status options are new, saved, applied, interviewing, offer, closed, and skipped. Omit --notes to keep existing notes.

### Excel workbook

Install the optional Excel export dependency once. Then run a search and refresh the workbook in the same command.

~~~powershell
python -m pip install -r requirements-excel.txt
python -m job_agent --db search.sqlite3 run --config settings.live.json --excel job-results.xlsx
~~~

The Job results tab has filters, scores, application status, notes, and links to postings. Estimated ATS measures the share of recognized product management keyword groups in the posting that also appear in your profile. It is a local estimate of keyword coverage. The score is blank when the posting contains fewer than five recognized groups. Run show to see the groups that matched or were missing. The Excluded tab holds U.S. jobs that failed a search rule. The workbook includes previously tracked jobs with U.S. location evidence. Older demo jobs and jobs without U.S. location evidence are left out. Use Last seen (UTC) to check when each was last returned by a source.

SQLite holds your application statuses and notes. Record changes with the status command, then run the export command below to refresh the workbook without another search. Excel edits are replaced on refresh. Close the workbook in Excel before refreshing it.

~~~powershell
python -m job_agent --db search.sqlite3 export job-results.xlsx
~~~

The app reads an optional .env file from your working directory. Shell variables take precedence. JOB_AGENT_CONFIG, JOB_AGENT_DB, and JOB_AGENT_RESUME set defaults. The matching CLI arguments take precedence. If no resume override is set, the profile path in settings is relative to the settings file.

The workflow is discovery → normalization and deduplication → deterministic eligibility → fit assessment → ranked SQLite tracker. Python's standard library handles the CLI, network requests, tests, and SQLite storage.
