"""Normalized job records and conservative identity keys."""

from dataclasses import dataclass, field, replace
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import json
import re
import unicodedata


DEMO_SOURCES = {"sample", "sample-mirror"}
US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()
STATE_SUFFIX = re.compile(r",\s*(?:" + "|".join(US_STATES) + r")(?=\s*[,;|/()–—-]|\s+\d{5}\b|\s*$)")


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[^\W_]+", text))


def location_names_us(location: str) -> bool:
    label = normalized(location)
    us = r"(?:united states(?: of america)?|us|usa|u s(?: a)?)"
    if re.search(rf"\b(?:outside|excluding|non|except|no)(?: the)? {us}\b", label):
        return False
    return bool(
        re.search(rf"\b{us}\b", label)
        or (re.search(r"\bcalifornia\b", label) and not re.search(r"\bbaja california\b", label))
        or STATE_SUFFIX.search(location)
        or label in {"nyc", "new york city", "san francisco", "san francisco bay area",
                     "los angeles", "seattle", "sacramento"}
    )


def address_country_is_us(country: str | None) -> bool | None:
    if not country:
        return None
    return normalized(country) in {"us", "u s", "usa", "u s a", "united states", "united states of america"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None:
        raise ValueError("Job URL must be an HTTP(S) URL without credentials")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != {"http": 80, "https": 443}[parts.scheme]:
        host = f"{host}:{port}"
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid", "gh_src"}
    ))
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        match = re.fullmatch(r"/([\w.-]+)/jobs/(\d+)/?", parts.path)
        if match:
            return f"https://job-boards.greenhouse.io/{match[1].lower()}/jobs/{match[2]}"
    # Preserve meaningful query parameters such as gh_jid, which can identify the job.
    route = parts.fragment if parts.fragment.startswith(("/", "!")) else ""
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, route))


@dataclass(frozen=True)
class Pay:
    minimum: int | None = None
    maximum: int | None = None
    currency: str = "USD"
    period: str = "year"

    def __post_init__(self):
        for value in (self.minimum, self.maximum):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Pay bounds must be positive whole currency units")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Pay minimum exceeds maximum")
        if not isinstance(self.currency, str) or not re.fullmatch("[A-Z]{3}", self.currency):
            raise ValueError("Pay currency must be a three-letter uppercase code")
        if self.period not in {"year", "month", "week", "day", "hour"}:
            raise ValueError("Unsupported pay period")

    def display(self) -> str:
        if self.minimum is None and self.maximum is None:
            return "unknown"
        minimum = f"{self.minimum:,}" if self.minimum is not None else "?"
        maximum = f"{self.maximum:,}" if self.maximum is not None else "?"
        return f"{self.currency} {minimum} to {maximum}/{self.period}"


@dataclass(frozen=True)
class Job:
    source: str
    source_id: str
    company: str
    title: str
    url: str
    description: str = ""
    location: str = ""
    work_mode: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    remote_ca: bool | None = None
    office_coordinates: tuple[float, float] | None = None
    base: Pay = field(default_factory=Pay)
    total: Pay = field(default_factory=Pay)
    description_is_excerpt: bool = False
    us_location: bool | None = None

    def __post_init__(self):
        for name in ("source", "source_id", "company", "title", "url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a nonempty string")
        for name in ("description", "location"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        canonical_url(self.url)
        if self.work_mode not in {"remote", "hybrid", "onsite", "unknown"}:
            raise ValueError("Unsupported work mode")
        if self.remote_ca is not None and type(self.remote_ca) is not bool:
            raise ValueError("remote_ca must be true, false, or null")
        if type(self.description_is_excerpt) is not bool:
            raise ValueError("description_is_excerpt must be true or false")
        if self.us_location is not None and type(self.us_location) is not bool:
            raise ValueError("us_location must be true, false, or null")
        if self.office_coordinates is not None:
            validate_coordinates(self.office_coordinates)
        if not isinstance(self.base, Pay) or not isinstance(self.total, Pay):
            raise ValueError("base and total must be pay records")

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        fields = dict(data)
        for name in ("base", "total"):
            fields[name] = Pay(**(fields.get(name) if fields.get(name) is not None else {}))
        if fields.get("office_coordinates") is not None:
            validate_coordinates(fields["office_coordinates"])
            fields["office_coordinates"] = tuple(fields["office_coordinates"])
        return cls(**fields)

    def identity_keys(self) -> tuple[str, str]:
        return (
            "source:" + json.dumps([self.source.strip().casefold(), self.source_id.strip()]),
            "url:" + canonical_url(self.url),
        )

    def is_demo(self) -> bool:
        return self.source.strip().casefold() in DEMO_SOURCES

    def is_live_us(self) -> bool:
        if self.is_demo():
            return False
        if self.us_location is not None:
            return self.us_location
        return (self.work_mode == "remote" and self.remote_ca is True) or location_names_us(self.location)


def validate_coordinates(coordinates):
    if not isinstance(coordinates, (tuple, list)) or len(coordinates) != 2:
        raise ValueError("Coordinates must be [latitude, longitude]")
    for value, limit in zip(coordinates, (90, 180)):
        if type(value) not in (float, int) or not -limit <= value <= limit:
            raise ValueError("Latitude or longitude is outside its valid range")


# Text-based inference for discovered postings. It only adds positive or explicit
# negative evidence; anything ambiguous stays unknown so the eligibility rules send it to review.
ROLE_SENTENCE = re.compile(r"\b(?:role|position|job|candidates?|applicants?|eligible|eligibility|reside|residents?|based|open to|must|available|hiring|location)\b")
CALIFORNIA = re.compile(r"\bcalifornia\b")
CA_EXCLUDED = re.compile(r"\b(?:not|except|excluding|excludes|outside|ineligible)\b(?: \w+){0,4} california\b")
REMOTE_US = re.compile(r"\b(?:anywhere in (?:the )?(?:us|united states)|remote (?:us|united states)|"
                       r"(?:us|united states) remote|remote within (?:the )?(?:us|united states))\b")
AMBIGUOUS_UNIT = re.compile(r"\b(?:hybrid|onsite|on site|in office|prefer|preferred|prefers)\b")
NEGATION = re.compile(r"\b(?:not|no|except|excluding|excludes|outside|cannot|ineligible)\b")
HYBRID_TEXT = re.compile(r"\bhybrid (?:role|position|job|work|schedule|model|arrangement|environment|setup|basis|structure|policy)\b"
                         r"|\b\d+ days? (?:a|per|each) week (?:in|at|from) (?:the |our |an )?office\b"
                         r"|\bin office \d+ days?\b")
ONSITE_TEXT = re.compile(r"\b(?:this|the|our) (?:role|position|job) (?:is|will be|requires?) (?:onsite|in office|in person|on site)\b"
                         r"|\bonsite (?:role|position|job)\b")


def infer_remote_ca(description: str) -> bool | None:
    # Scope is one line or bullet, because plain_text emits one line per HTML block.
    verdict = None
    for unit in re.split(r"[\r\n\u2022]+", description):
        text = normalized(unit)
        if "remote" not in text.split() or not ROLE_SENTENCE.search(text):
            continue
        # Location questions, long paragraphs, hybrid or onsite mentions, and "preferred" wording are ambiguous.
        if len(unit) > 300 or "?" in unit or AMBIGUOUS_UNIT.search(text):
            continue
        if CA_EXCLUDED.search(text):
            return False
        if NEGATION.search(text):
            continue
        if CALIFORNIA.search(text) or REMOTE_US.search(text):
            verdict = True
    return verdict


def infer_work_mode(description: str) -> str:
    text = normalized(description)
    hybrid, onsite = bool(HYBRID_TEXT.search(text)), bool(ONSITE_TEXT.search(text))
    if hybrid and not onsite:
        return "hybrid"
    if onsite and not hybrid:
        return "onsite"
    return "unknown"


def infer_from_description(job: "Job") -> "Job":
    mode, remote_ca = job.work_mode, job.remote_ca
    if mode == "unknown":
        mode = infer_work_mode(job.description)
    if remote_ca is None:
        remote_ca = infer_remote_ca(job.description)
        if remote_ca is True and mode == "unknown":
            mode = "remote"
        if remote_ca is True and mode in {"hybrid", "onsite"}:
            remote_ca = None  # Contradictory evidence stays unresolved.
    if (mode, remote_ca) == (job.work_mode, job.remote_ca):
        return job
    return replace(job, work_mode=mode, remote_ca=remote_ca)
