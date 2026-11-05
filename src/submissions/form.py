"""The per-event submission form: which built-in fields show, and the admin's custom questions.

A stage stores `submission_form = {"fields": [...]}`; NULL means the default form below, which is
exactly the form every event had before forms were configurable. Built-in fields stay real columns on
`submissions` (judging, exports and the leaderboard read them), so the config only says whether each
shows, what it is called and whether it is required. Custom answers live in `submissions.answers`.

A removed custom field is archived, not deleted, so the answers already given are kept (and still
exported); it is hidden from participants and judges.
"""

import math
import re
from urllib.parse import urlparse

BUILTIN_TYPES = {
    "project_name": "short_text",
    "github_url": "github",
    "pitch_deck_url": "url",
    "demo_url": "url",
    "video_url": "url",
    "documentation_url": "url",
    "description": "long_text",
}
BUILTIN_MAX = {"project_name": 200, "description": 1000}

# key, label, required, hint
DEFAULT_BUILTINS = [
    ("project_name", "Project name", True, ""),
    ("github_url", "GitHub repository", True, ""),
    ("pitch_deck_url", "Pitch deck link", True, "Share a Google Drive / Slides / Canva link anyone can view."),
    ("demo_url", "Live website / demo", False, "Shown on the public leaderboard once results are released."),
    ("video_url", "Demo video", False, ""),
    ("documentation_url", "Write-up / docs", False, ""),
    ("description", "Short description", False, "What it does, how you built it, the hardest problem you solved."),
]

CUSTOM_TYPES = ("short_text", "long_text", "url", "number", "select", "multi_select", "checkbox")
TEXT_LIMITS = {"short_text": (200, 500), "long_text": (2000, 5000)}
MAX_CUSTOM = 20
MAX_ARCHIVED = 40
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class FormError(ValueError):
    pass


def default_fields() -> list[dict]:
    return [
        {"key": key, "builtin": True, "type": BUILTIN_TYPES[key], "label": label, "hint": hint, "required": required,
         "enabled": True, "show_to_judges": True}
        for key, label, required, hint in DEFAULT_BUILTINS
    ]


def fields(stage) -> list[dict]:
    """Every field of the stage's form in order, archived custom fields included."""
    stored = (stage.submission_form or {}).get("fields") if stage is not None else None
    return [dict(item) for item in stored] if stored else default_fields()


def active(stage) -> list[dict]:
    """The fields participants fill in: enabled built-ins and non-archived custom fields."""
    return [item for item in fields(stage) if (item.get("enabled", True) if item.get("builtin") else not item.get("archived"))]


def form_payload(stage) -> dict:
    return {"fields": [_public(item) for item in active(stage)], "customized": bool(stage is not None and stage.submission_form)}


def _public(item: dict) -> dict:
    keys = ("key", "builtin", "type", "label", "hint", "required", "options", "max_length", "show_to_judges")
    return {key: item[key] for key in keys if key in item}


def _text(value, name: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise FormError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise FormError(f"{name} is required")
    if len(value) > limit:
        raise FormError(f"{name} must be at most {limit} characters")
    return value


def validate_config(raw: list[dict], previous: list[dict], answered_keys: set[str]) -> list[dict]:
    """Normalize an admin's form. `previous` is the stored form, `answered_keys` the custom keys teams have answered."""
    if not isinstance(raw, list):
        raise FormError("fields must be a list")
    before = {item["key"]: item for item in previous}
    seen: set[str] = set()
    result: list[dict] = []
    custom_count = 0
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise FormError(f"field {position + 1} is not an object")
        key = item.get("key")
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            raise FormError(f"field {position + 1}: key must be lowercase letters, digits or _ (starting with a letter)")
        if key in seen:
            raise FormError(f"field key {key} is used twice")
        seen.add(key)
        label = _text(item.get("label"), f"{key} label", 120, required=True)
        hint = _text(item.get("hint"), f"{label} hint", 300)
        required = bool(item.get("required", False))
        if key in BUILTIN_TYPES:
            enabled = bool(item.get("enabled", True))
            if key == "project_name":
                # Results, certificates and the leaderboard all need a project name.
                enabled, required = True, True
            result.append({"key": key, "builtin": True, "type": BUILTIN_TYPES[key], "label": label, "hint": hint,
                           "required": required and enabled, "enabled": enabled, "show_to_judges": True})
            continue
        kind = item.get("type")
        if kind not in CUSTOM_TYPES:
            raise FormError(f"{label}: type must be one of {', '.join(CUSTOM_TYPES)}")
        old = before.get(key)
        if old is not None and not old.get("builtin") and old.get("type") != kind and key in answered_keys:
            raise FormError(f"{label}: teams have already answered it, so its type cannot change; add a new field instead")
        archived = bool(item.get("archived", False))
        field = {"key": key, "builtin": False, "type": kind, "label": label, "hint": hint, "required": required and not archived,
                 "show_to_judges": bool(item.get("show_to_judges", True)), "archived": archived}
        if kind in TEXT_LIMITS:
            default, ceiling = TEXT_LIMITS[kind]
            limit = item.get("max_length", default)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= ceiling:
                raise FormError(f"{label}: max length must be between 1 and {ceiling}")
            field["max_length"] = limit
        if kind in ("select", "multi_select"):
            options = item.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 30:
                raise FormError(f"{label}: give between 1 and 30 options")
            cleaned = [_text(option, f"{label} option", 100, required=True) for option in options]
            if len(set(cleaned)) != len(cleaned):
                raise FormError(f"{label}: options must be different")
            field["options"] = cleaned
        if not archived:
            custom_count += 1
        result.append(field)
    for key in BUILTIN_TYPES:
        if key not in seen:
            raise FormError(f"the built-in field {key} is missing; turn it off instead of removing it")
    # A custom field dropped from the list is archived when teams answered it, so no answer is lost.
    for key, old in before.items():
        if key not in seen and not old.get("builtin") and key in answered_keys:
            result.append({**old, "archived": True, "required": False})
    if custom_count > MAX_CUSTOM:
        raise FormError(f"a form can have at most {MAX_CUSTOM} custom fields")
    if sum(1 for item in result if item.get("archived")) > MAX_ARCHIVED:
        raise FormError("too many removed fields; this form cannot take more changes")
    return result


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value is False


def _url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FormError(f"{name}: enter a full http(s) link")
    return value


def clean_answers(stage, raw: dict | None, stored: dict | None) -> dict:
    """Validate the answers a save sends (only active custom fields); keep the answers of archived fields."""
    if raw is None:
        return dict(stored or {})
    if not isinstance(raw, dict):
        raise FormError("answers must be an object")
    all_fields = fields(stage)
    custom = {item["key"]: item for item in active(stage) if not item.get("builtin")}
    unknown = sorted(set(raw) - set(custom))
    if unknown:
        raise FormError(f"unknown form fields: {', '.join(unknown)}")
    result = {key: value for key, value in (stored or {}).items()
              if any(item["key"] == key and item.get("archived") for item in all_fields)}
    for key, value in raw.items():
        field = custom[key]
        name = field["label"]
        kind = field["type"]
        if _empty(value):
            continue
        if kind in ("short_text", "long_text"):
            value = _text(value, name, field.get("max_length", TEXT_LIMITS[kind][0]))
            if not value:
                continue
        elif kind == "url":
            value = _url(_text(value, name, 500), name)
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e12:
                raise FormError(f"{name} must be a number")
        elif kind == "select":
            if value not in field["options"]:
                raise FormError(f"{name}: pick one of the options")
        elif kind == "multi_select":
            if not isinstance(value, list) or any(item not in field["options"] for item in value) or len(set(value)) != len(value):
                raise FormError(f"{name}: pick from the options")
            value = [option for option in field["options"] if option in value]
        elif kind == "checkbox":
            if not isinstance(value, bool):
                raise FormError(f"{name} must be ticked or not")
        result[key] = value
    return result


def missing_required(stage, submission) -> list[str]:
    """Labels of the required fields a submission has not filled, in form order."""
    answers = submission.answers or {}
    missing = []
    for field in active(stage):
        if not field.get("required"):
            continue
        value = getattr(submission, field["key"]) if field.get("builtin") else answers.get(field["key"])
        if _empty(value):
            missing.append(field["label"])
    return missing


def answer_text(field: dict, value) -> str:
    """One answer as plain text, for judges' cards and CSV cells."""
    if _empty(value):
        return ""
    if field.get("type") == "checkbox":
        return "Yes"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def judge_answers(stage, submission) -> list[dict]:
    """The custom answers a judge may see (fields marked show_to_judges), labelled, non-empty only."""
    answers = submission.answers or {}
    rows = []
    for field in active(stage):
        if field.get("builtin") or not field.get("show_to_judges", True):
            continue
        text = answer_text(field, answers.get(field["key"]))
        if text:
            rows.append({"key": field["key"], "label": field["label"], "type": field["type"], "value": text})
    return rows


def admin_answers(stage, submission) -> list[dict]:
    """Every custom answer for organisers, removed (archived) fields included and flagged."""
    answers = (submission.answers or {}) if submission is not None else {}
    rows = []
    for field in fields(stage):
        if field.get("builtin"):
            continue
        text = answer_text(field, answers.get(field["key"]))
        if text:
            rows.append({"key": field["key"], "label": field["label"], "type": field["type"], "value": text,
                         "show_to_judges": field.get("show_to_judges", True), "archived": bool(field.get("archived"))})
    return rows


LINK_LABELS = {"github_url": "Repository", "pitch_deck_url": "Pitch deck", "demo_url": "Website / demo", "video_url": "Video",
               "documentation_url": "Write-up"}


def builtin_links(stage, submission, labels: dict | None = None) -> list[dict]:
    """Links of the enabled built-in URL fields, in form order (the label is the form's, else the default)."""
    rows = []
    for field in active(stage):
        key = field["key"]
        if not field.get("builtin") or key not in LINK_LABELS:
            continue
        href = getattr(submission, key)
        if href:
            rows.append({"label": (labels or {}).get(key) or field["label"] or LINK_LABELS[key], "href": href})
    return rows


def field_enabled(stage, key: str) -> bool:
    return any(item["key"] == key for item in active(stage))
