from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class RegistrationPrice:
    amount_rupees: int
    amount_paise: int
    tier: str


class RegistrationCategory(StrEnum):
    ADAMAS_ACM = "Adamas ACM"
    ADAMAS_ONLY = "Adamas only"
    OUTER_COLLEGE = "Outer college"
    OUTER_ACM = "Outer + ACM"


def is_adamas(college: object) -> bool:
    return isinstance(college, str) and "adamas university" in college.strip().lower()


def calculate_registration_price(participants: list[dict[str, object]]) -> RegistrationPrice:
    if any(person.get("is_acm_member") is True and is_adamas(person.get("college")) for person in participants):
        amount, tier = 200, "adamas-acm"
    elif any(person.get("is_acm_member") is True for person in participants):
        amount, tier = 300, "acm"
    else:
        amount, tier = 400, "standard"
    return RegistrationPrice(amount, amount * 100, tier)


def effective_registration_price(members: Iterable[object]) -> RegistrationPrice:
    """Price from stored members, where an admin-rejected ACM claim no longer counts."""
    return calculate_registration_price([
        {
            "is_acm_member": bool(member.is_acm_member) and member.acm_review_status != "REJECTED",
            "college": member.college,
        }
        for member in members
    ])


def registration_category(tier: str, leader_college: str) -> RegistrationCategory:
    # Same predicate as pricing, so the admin category always explains the amount charged.
    if tier == "adamas-acm":
        return RegistrationCategory.ADAMAS_ACM
    if tier == "acm":
        return RegistrationCategory.OUTER_ACM
    if is_adamas(leader_college):
        return RegistrationCategory.ADAMAS_ONLY
    return RegistrationCategory.OUTER_COLLEGE
