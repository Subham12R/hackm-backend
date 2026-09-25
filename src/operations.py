def team_code(registration_id) -> str:
    return f"HM-{registration_id.hex[:6].upper()}"


def submission_label(status: str | None, evaluated: bool) -> str:
    if evaluated:
        return "Evaluated"
    return "Submitted" if status == "SUBMITTED" else "Draft"
