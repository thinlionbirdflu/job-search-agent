"""Eligibility checks independent of the fit scorer."""

from dataclasses import dataclass
from math import asin, cos, isfinite, radians, sin, sqrt
import re

from .models import Job, Pay, normalized, validate_coordinates


@dataclass(frozen=True)
class SearchPolicy:
    titles: tuple[str, ...]
    center: tuple[float, float]
    radius_miles: float = 50
    pay_target: int | None = 200_000
    pay_basis: str = "base"

    def __post_init__(self):
        if not isinstance(self.titles, (tuple, list)) or not self.titles or any(
            not isinstance(title, str) or not normalized(title) for title in self.titles
        ):
            raise ValueError("Set at least one title in filters.titles")
        validate_coordinates(self.center)
        if type(self.radius_miles) not in (int, float) or not isfinite(self.radius_miles) or self.radius_miles <= 0:
            raise ValueError("radius_miles must be positive and finite")
        if self.pay_target is not None and (type(self.pay_target) is not int or self.pay_target <= 0):
            raise ValueError("pay_target must be a positive integer or null")
        if self.pay_basis not in {"base", "total"}:
            raise ValueError("pay_basis must be base or total")


@dataclass(frozen=True)
class Check:
    state: str
    reason: str


@dataclass(frozen=True)
class Eligibility:
    state: str
    checks: dict[str, Check]


def distance_miles(start, end) -> float:
    lat1, lon1 = map(radians, start)
    lat2, lon2 = map(radians, end)
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 3958.7613 * 2 * asin(sqrt(min(1, a)))


def check_location(job: Job, policy: SearchPolicy) -> Check:
    if job.work_mode == "remote":
        if job.remote_ca is True:
            return Check("pass", "Remote eligibility includes California")
        if job.remote_ca is False:
            return Check("fail", "Remote eligibility excludes California")
        return Check("review", "Confirm remote eligibility from California")
    if job.work_mode == "hybrid":
        if job.office_coordinates is None:
            return Check("review", "Hybrid office coordinates are missing")
        miles = distance_miles(policy.center, job.office_coordinates)
        state = "pass" if miles <= policy.radius_miles else "fail"
        return Check(state, f"Hybrid office is {miles:.1f} straight-line miles from the search center "
                            f"(limit {policy.radius_miles:g})")
    if job.work_mode == "onsite":
        return Check("fail", "Office-only role")
    return Check("review", "Work arrangement is unconfirmed")


def check_pay(pay: Pay, policy: SearchPolicy) -> Check:
    if policy.pay_target is None:
        return Check("pass", "Compensation filter is off")
    target = f"${policy.pay_target:,}"
    basis = f"annual {policy.pay_basis} pay"
    if pay.minimum is None and pay.maximum is None:
        return Check("review", f"No verified {basis} range")
    if pay.currency != "USD" or pay.period != "year":
        return Check("review", f"{basis.capitalize()} needs an annual USD figure")
    if pay.maximum is not None and pay.maximum <= policy.pay_target:
        return Check("fail", f"{basis.capitalize()} tops out at or below {target}")
    if pay.minimum is not None and pay.minimum > policy.pay_target:
        return Check("pass", f"{basis.capitalize()} floor exceeds {target}")
    return Check("review", f"Verify whether the offer can exceed {target} in {basis}")


# Common title abbreviations, expanded before comparison: "Sr Product Manager" == "Senior Product Manager".
TITLE_ALIASES = {"sr": "senior", "pm": "product manager"}
# Seniority signals that the title gate rejects. "Associate Director" is a senior title, so it is exempt.
JUNIOR_TITLE = re.compile(r"\b(?:associate|junior|jr|intern|internship|entry level|apprentice)\b(?! director)")


def title_forms(text: str) -> set[str]:
    base = normalized(text)
    expanded = " ".join(TITLE_ALIASES.get(word, word) for word in base.split())
    return {base, expanded}


def evaluate(job: Job, policy: SearchPolicy) -> Eligibility:
    allowed = set().union(*(title_forms(title) for title in policy.titles))
    boundaries = re.finditer(r"\s[-–—|]\s|[:,(]", job.title)
    candidates = [job.title, *(job.title[:match.start()] for match in boundaries)]
    title_match = any(title_forms(candidate) & allowed for candidate in candidates)
    junior = bool(JUNIOR_TITLE.search(normalized(job.title)))
    checks = {
        "title": Check("fail", "Title is outside the configured roles") if not title_match
                 else Check("fail", "Title signals a junior level") if junior
                 else Check("pass", "Title matches a configured role"),
        "location": check_location(job, policy),
        "compensation": check_pay(getattr(job, policy.pay_basis), policy),
    }
    states = {check.state for check in checks.values()}
    state = "excluded" if "fail" in states else "review" if "review" in states else "eligible"
    return Eligibility(state, checks)
