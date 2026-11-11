"""Write a readable Excel snapshot of the SQLite tracker."""

from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from textwrap import wrap

from .models import Job
from .tracker import ATS_OVERRIDE, MINIMUM_FIT, STATES


HEADERS = (
    "Job ID", "Company", "Role", "Result", "Fit / 100", "Estimated ATS / 100", "Missing Keywords", "Application status",
    "Posting", "Location", "Work mode", "Base pay", "Total compensation",
    "Decision details", "Notes", "Source", "Last seen (UTC)",
)
WIDTHS = (9, 23, 39, 12, 12, 23, 42, 19, 13, 25, 12, 24, 24, 56, 36, 22, 22)
STATE_COLORS = {
    "eligible": ("#E4F3E9", "#165C37"),
    "review": ("#FFF1CD", "#7A500B"),
    "rejected": ("#FCE8E6", "#9B3230"),
    "pending": ("#E8F1FA", "#224970"),
    "excluded": ("#EDF0F4", "#4B5563"),
}


def decision_details(row, fit, excerpt, ats_score):
    checks = json.loads(row["checks"]) if row["checks"] else {}
    details = []
    if row["state"] == "rejected":
        details.append(f"Fit score below {MINIMUM_FIT}")
    elif row["score"] is not None and row["score"] < MINIMUM_FIT and ats_score is not None and ats_score > ATS_OVERRIDE:
        details.append(f"Included with estimated ATS above {ATS_OVERRIDE}")
    if fit and fit["score"] is not None:
        method = fit["method"].replace("_", " ")
        details.append(f"Scored with {method}" + (" using an excerpt" if excerpt else ""))
    if fit and fit.get("warning"):
        details.append(fit["warning"])
    details.extend(check["reason"] for check in checks.values() if check["state"] != "pass")
    return " | ".join(details)


def write_sheet(book, name, rows, caption):
    sheet = book.add_worksheet(name)
    sheet.hide_gridlines(2)
    sheet.freeze_panes(4, 3)
    navy, slate = "#173352", "#42546B"
    title = book.add_format({"bold": True, "font_size": 18, "font_color": navy, "valign": "vcenter"})
    subtitle = book.add_format({"font_color": slate, "valign": "vcenter"})
    header = book.add_format({"bold": True, "bg_color": navy, "font_color": "#FFFFFF", "valign": "vcenter"})
    body = book.add_format({"font_color": navy, "valign": "vcenter", "text_wrap": True})
    score = book.add_format({"align": "center", "valign": "vcenter", "num_format": "0.0"})
    timestamp = book.add_format({"valign": "vcenter", "num_format": "yyyy-mm-dd hh:mm"})
    link = book.add_format({"font_color": "#1759A8", "underline": True, "valign": "vcenter"})
    tags = {
        state: book.add_format({"bg_color": bg, "font_color": fg, "bold": True,
                                "align": "center", "valign": "vcenter"})
        for state, (bg, fg) in STATE_COLORS.items()
    }

    for column, width in enumerate(WIDTHS):
        sheet.set_column(column, column, width)
    sheet.set_row(0, 29)
    sheet.set_row(1, 20)
    sheet.set_row(3, 25)
    sheet.merge_range(0, 0, 0, 6, name, title)
    sheet.merge_range(1, 0, 1, 8, caption, subtitle)
    sheet.write_row(3, 0, HEADERS, header)
    sheet.autofilter(3, 0, 3 + len(rows), len(HEADERS) - 1)

    for index, row in enumerate(rows, start=4):
        job = Job.from_dict(json.loads(row["payload"]))
        fit = json.loads(row["fit"]) if row["fit"] else None
        ats = (fit or {}).get("ats") or {}
        ats_score = ats.get("score")
        missing_keywords = ", ".join(ats.get("missing", ()))
        values = (
            row["id"], job.company, job.title, row["state"].title(), row["score"], ats_score, missing_keywords,
            row["status"].title(), None, job.location or "Unspecified", job.work_mode.title(),
            job.base.display(), job.total.display(),
            decision_details(row, fit, job.description_is_excerpt, ats_score), row["notes"],
            job.source, datetime.fromisoformat(row["last_seen"]).replace(tzinfo=None),
        )
        sheet.set_row(index, max(32, 15 * len(wrap(missing_keywords, width=38))))
        sheet.write_row(index, 0, values, body)
        sheet.write(index, 3, values[3], tags[row["state"]])
        sheet.write(index, 4, row["score"], score)
        sheet.write(index, 5, ats_score, score)
        if len(job.url) <= 2079:
            sheet.write_url(index, 8, job.url, link, "Open posting")
        else:
            sheet.write_string(index, 8, job.url, body)
        sheet.write_datetime(index, 16, values[16], timestamp)


def export_workbook(path: Path, rows):
    try:
        import xlsxwriter
    except ImportError:
        raise ValueError("Excel export needs XlsxWriter. Run python -m pip install -r requirements-excel.txt") from None

    counts = Counter(row["state"] for row in rows)
    results = [row for row in rows if row["state"] != "excluded"]
    excluded = [row for row in rows if row["state"] == "excluded"]
    summary = "  |  ".join(f"{counts[state]} {state}" for state in STATES if state != "excluded")
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".xlsx", delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        with xlsxwriter.Workbook(str(staged), {
            "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
        }) as book:
            write_sheet(book, "Job results", results,
                        summary + f"  |  Fit {MINIMUM_FIT}+ or estimated ATS above {ATS_OVERRIDE}")
            write_sheet(book, "Excluded", excluded,
                        f"{counts['excluded']} excluded by the title, location, or enabled pay rules")
        try:
            os.replace(staged, path)
        except PermissionError:
            raise ValueError(f"Could not replace {path}. Close the workbook in Excel and try exporting again.") from None
    finally:
        staged.unlink(missing_ok=True)
