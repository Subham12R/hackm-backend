from dataclasses import dataclass


@dataclass(frozen=True)
class RegistrationPrice:
    amount_rupees: int
    amount_paise: int
    tier: str


def calculate_registration_price(participants: list[dict[str, object]]) -> RegistrationPrice:
    def adamas(college: object) -> bool:
        return isinstance(college, str) and "adamas university" in college.strip().lower()

    if any(person.get("is_acm_member") is True and adamas(person.get("college")) for person in participants):
        amount, tier = 200, "adamas-acm"
    elif any(person.get("is_acm_member") is True for person in participants):
        amount, tier = 300, "acm"
    else:
        amount, tier = 400, "standard"
    return RegistrationPrice(amount, amount * 100, tier)
