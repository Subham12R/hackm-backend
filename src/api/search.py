"""One search box behaviour for every admin table.

Staff type a team name, a leader email, a team code (HM-1A2B3C) or, on judging pages, a submission
code. Matching is a case-insensitive substring match; the team code matches the start of the
registration id, which is where `team_code` takes its six characters from.
"""

import re

from sqlalchemy import String, cast, false, func, or_

from src.db.models.registration import Registration, Submission

TEAM_CODE = re.compile(r"^(?:hm-?)?([0-9a-f]{3,32})$")


def needle(term: str | None) -> str:
    return (term or "").strip().lower()


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def contains(column, term: str):
    return func.lower(column).like(_like(term), escape="\\")


def team_code_match(term: str):
    """`HM-1A2B3C` (or a bare hex prefix of at least 3 characters) against the registration id."""
    match = TEAM_CODE.match(term.replace(" ", ""))
    if match is None:
        return false()
    return cast(Registration.id, String).like(f"{match.group(1)}%")


def team_search(term: str | None):
    """Team name, leader email or team code. None when the box is empty."""
    value = needle(term)
    if not value:
        return None
    return or_(contains(Registration.team_name, value), contains(Registration.leader_email, value), team_code_match(value))


def submission_search(term: str | None, *, with_team: bool = True):
    """Submission code and project name, plus the team fields when the viewer may see identities."""
    value = needle(term)
    if not value:
        return None
    clauses = [contains(Submission.anonymous_code, value), contains(Submission.project_name, value)]
    if with_team:
        clauses += [contains(Registration.team_name, value), team_code_match(value)]
    return or_(*clauses)
