from src.registrations.pricing import calculate_registration_price


def test_pricing_matches_the_participant_frontend_rules() -> None:
    adamas = calculate_registration_price([
        {"college": "Adamas University", "is_acm_member": True, "acm_id": "ACM123"},
        {"college": "Other College", "is_acm_member": False},
    ])
    acm = calculate_registration_price([
        {"college": "Other College", "is_acm_member": True, "acm_id": "ACM123"},
    ])
    standard = calculate_registration_price([
        {"college": "Adamas University", "is_acm_member": False},
    ])

    assert (adamas.amount_rupees, adamas.tier) == (200, "adamas-acm")
    assert (acm.amount_rupees, acm.tier) == (300, "acm")
    assert (standard.amount_rupees, standard.tier) == (400, "standard")
    assert adamas.amount_paise == 20000
