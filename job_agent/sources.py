"""Discovery adapters. Each yields normalized jobs with explicit unknowns."""

from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Job, address_country_is_us, infer_from_description, location_names_us, normalized


def get_json(url: str, *, payload=None, api_key=None, extra_headers=None):
    headers = {"Accept": "application/json", "User-Agent": "local-job-search-agent/1.0"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(extra_headers or {})
    request = Request(url, data=data, headers=headers)
    # Retry read-only discovery requests on timeouts and server errors. Model calls (payload set) never retry.
    attempts = 3 if payload is None else 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=25) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code < 500 or attempt == attempts - 1:
                raise
        except (TimeoutError, URLError):
            if attempt == attempts - 1:
                raise
        time.sleep(2 * (attempt + 1))


class JobText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = False

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def plain_text(content: str) -> str:
    parser = JobText()
    parser.feed(unescape(content))
    parser.close()
    return "\n".join(parser.parts)


def work_mode_from_label(label: str) -> str:
    label = normalized(label)
    modes = [mode for mode, pattern in (
        ("hybrid", r"\bhybrid\b"),
        ("onsite", r"\b(?:on site|onsite|in office)\b"),
        ("remote", r"\bremote\b"),
    ) if re.search(pattern, label)]
    return modes[0] if len(modes) == 1 else "unknown"


def json_jobs(spec: dict, directory: Path) -> list[Job]:
    path = directory / spec["path"]
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must contain a JSON array")
    jobs = []
    for number, row in enumerate(data, start=1):
        try:
            if not isinstance(row, dict):
                raise ValueError("Expected a JSON object")
            job = Job.from_dict(row)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{path.name}, row {number}: {exc}") from exc
        jobs.append(job)
    return jobs


def board_details(spec: dict) -> tuple[str, str]:
    board, company = spec["board"], spec["company"]
    if not isinstance(board, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", board) or board in {".", ".."}:
        raise ValueError("Board must be a public employer board token")
    return board, company


def greenhouse_jobs(spec: dict, directory: Path) -> list[Job]:
    board, company = board_details(spec)
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
    data = get_json(url)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Greenhouse response did not contain a jobs array")
    jobs = []
    for row in data["jobs"]:
        if "internal_job_id" in row and row["internal_job_id"] is None:
            continue  # Greenhouse uses a null internal ID for prospect posts.
        location = (row.get("location") or {}).get("name") or ""
        jobs.append(Job(
            source=f"greenhouse:{board.lower()}", source_id=str(row["id"]),
            company=company, title=row["title"], url=row["absolute_url"],
            description=plain_text(row.get("content") or ""), location=location,
            work_mode=work_mode_from_label(location),
        ))
    return jobs


def ashby_jobs(spec: dict, directory: Path) -> list[Job]:
    board, company = board_details(spec)
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError("Ashby response did not contain a jobs array")
    jobs = []
    for row in data["jobs"]:
        if row.get("isListed") is False:
            continue
        url = row["jobUrl"]
        location = row.get("location") or ""
        primary = (row.get("address") or {}).get("postalAddress") or {}
        places = [(location, primary)]
        places.extend((item.get("location") or "", item.get("address") or {})
                      for item in row.get("secondaryLocations") or [])
        us_locations = []
        for label, address in places:
            country = address_country_is_us(address.get("addressCountry"))
            us_locations.append(location_names_us(label) if country is None else country)
        mode = {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "onsite"}.get(row.get("workplaceType"))
        jobs.append(Job(
            source=f"ashby:{board.lower()}", source_id=url, company=company,
            title=row["title"], url=url, location="; ".join(dict.fromkeys(label for label, _ in places if label)),
            description=row.get("descriptionPlain") or plain_text(row.get("descriptionHtml") or ""),
            work_mode=mode or work_mode_from_label(location), us_location=any(us_locations),
        ))
    return jobs


def lever_jobs(spec: dict, directory: Path) -> list[Job]:
    board, company = board_details(spec)
    region = spec.get("region", "global")
    if region not in ("global", "eu"):
        raise ValueError("Lever region must be global or eu")
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    jobs = []
    seen = set()
    page_size = 100
    for offset in range(0, 10_000, page_size):
        rows = get_json(f"https://{host}/v0/postings/{board}?mode=json&skip={offset}&limit={page_size}")
        if not isinstance(rows, list):
            raise ValueError("Lever response did not contain a postings array")
        if len(rows) == page_size and all(row["id"] in seen for row in rows):
            raise ValueError("Lever returned a repeated page")
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            categories = row.get("categories") or {}
            location = categories.get("location") or ""
            alternatives = categories.get("allLocations") or []
            country = address_country_is_us(row.get("country"))
            primary_us = location_names_us(location) if country is None else country
            us_location = primary_us or any(
                location_names_us(label) for label in alternatives if normalized(label) != normalized(location)
            )
            mode = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite"}.get(row.get("workplaceType"))
            description = row.get("descriptionPlain") or plain_text(row.get("description") or "")
            details = [description, *(plain_text(item.get("content") or "") for item in row.get("lists") or [])]
            jobs.append(Job(
                source=f"lever:{region}:{board.lower()}", source_id=row["id"], company=company,
                title=row["text"], url=row["hostedUrl"],
                description="\n".join(part for part in details if part),
                location="; ".join(dict.fromkeys(label for label in [location, *alternatives] if label)),
                work_mode=mode or work_mode_from_label(location), us_location=us_location,
            ))
        if len(rows) < page_size:
            return jobs
    raise ValueError("Lever returned more than 10,000 postings for this board")


def adzuna_jobs(spec: dict, directory: Path) -> list[Job]:
    app_id = os.getenv("ADZUNA_APP_ID", "").strip()
    app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        raise ValueError("Set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env to search Adzuna")

    title = spec.get("title")
    if not isinstance(title, str) or not normalized(title):
        raise ValueError("Set a title for each Adzuna search")
    pages = spec.get("pages", 1)
    page_size = spec.get("results_per_page", 50)
    if type(pages) is not int or not 1 <= pages <= 10:
        raise ValueError("Adzuna pages must be an integer from 1 to 10")
    if type(page_size) is not int or not 1 <= page_size <= 50:
        raise ValueError("Adzuna results_per_page must be an integer from 1 to 50")

    params = {"app_id": app_id, "app_key": app_key, "title_only": title, "results_per_page": page_size}
    for field in ("what", "where"):
        if field in spec:
            value = spec[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Adzuna {field} must be nonempty text")
            params[field] = value
    if "distance_km" in spec:
        distance = spec["distance_km"]
        if "where" not in params or type(distance) is not int or distance < 1:
            raise ValueError("Adzuna distance_km must be a positive integer and requires where")
        params["distance"] = distance

    jobs = []
    for page in range(1, pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/us/search/{page}?{urlencode(params)}"
        # The provider requires query-string credentials. Keep request URLs out of diagnostics.
        try:
            data = get_json(url)
        except HTTPError as exc:
            raise ValueError(f"Adzuna request failed with HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise ValueError(f"Adzuna request failed ({type(exc).__name__})") from None
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Adzuna response did not contain a results array")
        rows = data["results"]
        for row in rows:
            location = (row.get("location") or {}).get("display_name") or ""
            description = plain_text(row.get("description") or "")
            jobs.append(Job(
                source="adzuna:us", source_id=str(row["id"]), title=row["title"],
                company=(row.get("company") or {}).get("display_name") or "Employer unspecified",
                url=row["redirect_url"], description=description, location=location,
                work_mode=work_mode_from_label(location), description_is_excerpt=bool(description),
            ))
        if len(rows) < page_size or (type(data.get("count")) is int and page * page_size >= data["count"]):
            break
    return jobs


def recruitee_jobs(spec: dict, directory: Path) -> list[Job]:
    # Unverified against live endpoints: covered by fixture tests only.
    slug, company = spec.get("slug"), spec["company"]
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError("Recruitee slug must be the company's lowercase subdomain")
    data = get_json(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        raise ValueError("Recruitee response did not contain an offers array")
    jobs = []
    for row in data["offers"]:
        location = ", ".join(dict.fromkeys(
            part for part in (row.get("city"), row.get("state_name"), row.get("country")) if part
        ))
        if row.get("hybrid") is True:
            mode = "hybrid"
        elif row.get("remote") is True:
            mode = "remote"
        else:
            mode = work_mode_from_label(location)
        content = "\n".join(part for part in (row.get("description"), row.get("requirements")) if part)
        jobs.append(Job(
            source=f"recruitee:{slug}", source_id=str(row["id"]), company=company,
            title=row["title"], url=row["careers_url"], description=plain_text(content),
            location=location, work_mode=mode,
            us_location=address_country_is_us(row.get("country_code")),
        ))
    return jobs


SMARTRECRUITERS_PAGE = 100


def smartrecruiters_jobs(spec: dict, directory: Path) -> list[Job]:
    # Unverified against live endpoints: covered by fixture tests only.
    company_id, company = spec.get("company_id"), spec["company"]
    if not isinstance(company_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", company_id) or company_id in {".", ".."}:
        raise ValueError("SmartRecruiters company_id must be the company identifier")
    base = f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
    summaries, offset = [], 0
    while offset < 10_000:
        data = get_json(f"{base}?limit={SMARTRECRUITERS_PAGE}&offset={offset}")
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ValueError("SmartRecruiters response did not contain a content array")
        page = data["content"]
        summaries.extend(page)
        offset += len(page)
        total = data.get("totalFound")
        if len(page) < SMARTRECRUITERS_PAGE or (type(total) is int and offset >= total):
            break
    jobs = []
    for summary in summaries:
        posting_id = str(summary["id"])
        detail = get_json(f"{base}/{posting_id}")
        sections = ((detail.get("jobAd") or {}).get("sections") or {}).values()
        description = "\n".join(plain_text(section.get("text") or "") for section in sections if isinstance(section, dict))
        location = summary.get("location") or {}
        label = location.get("fullLocation") or ", ".join(dict.fromkeys(
            part for part in (location.get("city"), location.get("region"), location.get("country")) if part
        ))
        mode = "remote" if location.get("remote") is True else work_mode_from_label(label)
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company_id}/{posting_id}"
        jobs.append(Job(
            source=f"smartrecruiters:{company_id.lower()}", source_id=posting_id, company=company,
            title=summary["name"], url=url, description=description, location=label, work_mode=mode,
            us_location=address_country_is_us(location.get("country")),
        ))
    return jobs


ADAPTERS = {"json": json_jobs, "greenhouse": greenhouse_jobs, "ashby": ashby_jobs,
            "lever": lever_jobs, "adzuna": adzuna_jobs, "recruitee": recruitee_jobs,
            "smartrecruiters": smartrecruiters_jobs}
DEFAULT_PRIORITY = {"json": 100, "greenhouse": 10, "ashby": 10, "lever": 10, "adzuna": 5,
                    "recruitee": 10, "smartrecruiters": 10}


def discover(spec: dict, directory: Path) -> tuple[list[Job], int]:
    kind = spec["type"]
    if kind not in ADAPTERS:
        raise ValueError(f"Unknown source type {kind!r}")
    priority = spec.get("priority", DEFAULT_PRIORITY[kind])
    if type(priority) is not int:
        raise ValueError("Source priority must be an integer")
    jobs = ADAPTERS[kind](spec, directory)
    if kind != "json":
        # Hand-entered JSON records are kept as written; discovered postings get text-based inference.
        jobs = [infer_from_description(job) for job in jobs]
    return jobs, priority
