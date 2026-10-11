import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

# What each tier must net after Razorpay's cut.
TIER_BASE_PAISE = {"adamas-acm": 20000, "acm": 30000, "adamas": 30000, "standard": 40000}


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
    elif any(is_adamas(person.get("college")) for person in participants):
        amount, tier = 300, "adamas"
    else:
        amount, tier = 400, "standard"
    return RegistrationPrice(amount, amount * 100, tier)


def effective_registration_price(members: Iterable[object]) -> RegistrationPrice:
    """Price from stored members, where an admin-rejected ACM claim no longer counts."""
    return calculate_registration_price([
        {
            # A rejected claim stays undiscounted while a corrected ID awaits re-review (RECHECK).
            "is_acm_member": bool(member.is_acm_member) and member.acm_review_status not in ("REJECTED", "RECHECK"),
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
    if tier == "adamas":
        return RegistrationCategory.ADAMAS_ONLY
    return RegistrationCategory.OUTER_COLLEGE


def processing_fee_ppm() -> int:
    """Razorpay's cut in parts per million of the amount charged: fee % plus GST on that fee.

    Defaults to 2% + 18% GST = 2.36% (domestic cards, UPI, netbanking). RAZORPAY_FEE_PERCENT=0 turns
    the pass-through off, for example if Razorpay's own "customer fee bearer" mode is enabled.
    """
    fee = float(os.getenv("RAZORPAY_FEE_PERCENT", "2"))
    gst = float(os.getenv("RAZORPAY_FEE_GST_PERCENT", "18"))
    ppm = round(fee / 100 * (1 + gst / 100) * 1_000_000)
    return min(max(ppm, 0), 500_000)


def with_processing_fee(base_paise: int, ppm: int) -> int:
    """The amount to charge so that, after Razorpay keeps `ppm` of it, at least `base_paise` arrives.

    Rounded up to a whole rupee: ₹200 -> ₹205, ₹300 -> ₹308, ₹400 -> ₹410 at 2.36%.
    """
    if base_paise <= 0 or ppm <= 0:
        return base_paise
    gross = math.ceil(base_paise * 1_000_000 / (1_000_000 - ppm))
    return math.ceil(gross / 100) * 100
