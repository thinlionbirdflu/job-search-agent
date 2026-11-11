"""Read a JSON settings file and optional local environment defaults."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from .filters import SearchPolicy
from .scoring import RUBRICS


def load_env(path: Path = Path(".env")):
    if not path.is_file():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid .env assignment on line {number}")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[0] != value[-1]:
                raise ValueError(f"Unclosed .env quote on line {number}")
            value = value[1:-1]
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class Settings:
    policy: SearchPolicy
    profile: Path
    sources: list[dict]
    directory: Path
    fit_rubric: str

    @classmethod
    def read(cls, path: Path) -> "Settings":
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("Settings must be a JSON object")
        sources = config["sources"]
        if not isinstance(sources, list) or not sources or not all(isinstance(row, dict) for row in sources):
            raise ValueError("Settings need a nonempty sources list")
        rubric = config.get("fit_rubric", "commerce")
        if not isinstance(rubric, str) or rubric not in RUBRICS:
            raise ValueError("fit_rubric must be commerce or general_product")
        return cls(SearchPolicy(**config["filters"]), path.parent / config["profile"], sources, path.parent, rubric)
