"""HTML rendering for the watch page, the office page and emails (Jinja2, autoescaped)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Zest's logo colors, sampled from the full-colour logo (see the build brief).
BRAND = {
    "purple": "#6E20B9",
    "bright_purple": "#932FFB",
    "violet": "#5600C6",
    "yellow": "#FFF515",
    "lime": "#A7E84C",
    "mint": "#6FE7C8",
    "black": "#000000",
    "white": "#FFFFFF",
}


def _time(value: datetime | None, tz: ZoneInfo, fmt: str = "%-I:%M %p") -> str:
    return value.astimezone(tz).strftime(fmt) if value else ""


def _day(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%b %-d, %Y") if value else ""


def _nice_date(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %-d")
    except ValueError:
        return day


def _phone(value: str) -> str:
    """+19165550100 -> (916) 555-0100 for reading; the tel: link keeps the raw number."""
    digits = "".join(ch for ch in value or "" if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return value or ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


_env.filters["phone"] = _phone
_env.filters["time"] = _time
_env.filters["day"] = _day
_env.filters["nice_date"] = _nice_date
_env.globals["brand_colors"] = BRAND


def render(template: str, **context: Any) -> str:
    return _env.get_template(template).render(**context)
