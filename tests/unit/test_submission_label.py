from uuid import UUID

from src.operations import submission_label, team_code


def test_submission_table_labels_and_team_codes():
    assert submission_label(None, False) == "Draft"
    assert submission_label("DRAFT", False) == "Draft"
    assert submission_label("SUBMITTED", False) == "Submitted"
    assert submission_label("SUBMITTED", True) == "Evaluated"
    assert team_code(UUID("00000000-0000-4000-8000-0000000000ab")) == "HM-000000"
