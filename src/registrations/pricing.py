import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
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


DISCOUNT_KINDS = ("NONE", "PERCENT", "AMOUNT")
# Discounts stay below the cheapest tier, so a discounted price is never zero (Razorpay needs at least ₹1).
MAX_DISCOUNT_PERCENT = 99
MAX_DISCOUNT_PAISE = min(TIER_BASE_PAISE.values()) - 100


@dataclass(frozen=True)
class Discount:
    kind: str
    value: int
    label: str = ""


def apply_discount(base_paise: int, kind: str | None, value: int | None) -> int:
    """The entry fee after a percent or fixed-amount discount, rounded to a rupee and never below ₹1."""
    if base_paise <= 0 or not value or kind not in ("PERCENT", "AMOUNT"):
        return base_paise
    off = base_paise * value // 100 if kind == "PERCENT" else value
    return max(100, (base_paise - off) // 100 * 100)


def active_discount(event: object, now: datetime) -> Discount | None:
    """The event's discount if it is switched on and `now` is inside its window (unset bounds never block)."""
    kind = getattr(event, "discount_kind", "NONE") or "NONE"
    value = getattr(event, "discount_value", 0) or 0
    if kind == "NONE" or value <= 0:
        return None
    starts, ends = getattr(event, "discount_starts_at", None), getattr(event, "discount_ends_at", None)
    if (starts is not None and now < starts) or (ends is not None and now >= ends):
        return None
    return Discount(kind, value, getattr(event, "discount_label", "") or "")


def discount_state(event: object, now: datetime) -> str:
    """OFF, SCHEDULED (starts later), ACTIVE or ENDED, for organiser and registration pages."""
    kind = getattr(event, "discount_kind", "NONE") or "NONE"
    if kind == "NONE" or not (getattr(event, "discount_value", 0) or 0):
        return "OFF"
    starts, ends = getattr(event, "discount_starts_at", None), getattr(event, "discount_ends_at", None)
    if ends is not None and now >= ends:
        return "ENDED"
    if starts is not None and now < starts:
        return "SCHEDULED"
    return "ACTIVE"


def entry_fee_paise(tier: str, fixed_price_minor: int | None, discount_kind: str | None, discount_value: int | None) -> int:
    """What a team's entry costs before gateway charges: an invite's fixed price, else its tier less any discount."""
    if fixed_price_minor:
        return fixed_price_minor
    return apply_discount(TIER_BASE_PAISE.get(tier, TIER_BASE_PAISE["standard"]), discount_kind, discount_value)


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
