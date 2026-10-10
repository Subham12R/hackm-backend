from sqlalchemy import select

from src.auth.policies import RoleCode
from src.db.models.registration import Registration, RegistrationMember
from src.registrations.pricing import processing_fee_ppm, with_processing_fee
from tests.helpers import Harness, db, staff


def razorpay_net(charged_paise: int) -> int:
    """What Razorpay settles: the charge minus 2% fee and 18% GST on that fee, each rounded to the paisa."""
    fee = round(charged_paise * 0.02)
    return charged_paise - fee - round(fee * 0.18)


def test_charge_covers_the_fee_for_every_tier(monkeypatch) -> None:
    monkeypatch.delenv("RAZORPAY_FEE_PERCENT", raising=False)
    monkeypatch.delenv("RAZORPAY_FEE_GST_PERCENT", raising=False)
    ppm = processing_fee_ppm()
    assert ppm == 23_600
    assert [with_processing_fee(base, ppm) for base in (20000, 30000, 40000)] == [20500, 30800, 41000]
    for base in range(100, 100_001, 100):
        assert razorpay_net(with_processing_fee(base, ppm)) >= base
    assert with_processing_fee(20000, 0) == 20000 and with_processing_fee(0, ppm) == 0
    monkeypatch.setenv("RAZORPAY_FEE_PERCENT", "0")
    assert processing_fee_ppm() == 0


def _acm_member(team_name: str):
    with db() as session:
        registration = session.scalar(select(Registration).where(Registration.team_name == team_name))
        member = session.scalar(select(RegistrationMember).where(RegistrationMember.registration_id == registration.id, RegistrationMember.is_acm_member))
        return registration, member


def test_new_teams_pay_the_fee_and_older_teams_keep_their_price(monkeypatch) -> None:
    harness = Harness()
    admin = harness.login(staff(RoleCode.REGISTRATION_ADMIN), "admin")
    # Registered before the pass-through existed (fee off): its balance must not grow afterwards.
    old_payload, old_leader = harness.paid_team()

    monkeypatch.setenv("RAZORPAY_FEE_PERCENT", "2")
    payload, created = harness.register()
    assert created.status_code == 202
    checkout = created.json()
    assert (checkout["amount"], checkout["base_amount"], checkout["fee_amount"], checkout["tier"]) == (20500, 20000, 500, "adamas-acm")
    assert checkout["fee_percent"] == 2.36
    payment = harness.gateway.pay(checkout["order_id"], checkout["amount"])
    assert harness.confirm(payment).status_code == 200
    leader = harness.activate_leader(payload["members"][0]["email"])
    assert harness.client.get("/api/v1/participant/dashboard", headers=leader).json()["amount_minor"] == 20500

    # A rejected ACM claim moves the team to the ₹400 tier; the balance keeps the fee so the total still nets ₹400.
    registration, member = _acm_member(payload["team_name"])
    rejected = harness.client.post(f"/api/v1/admin/teams/{registration.id}/members/{member.id}/acm-review", headers=admin, json={"status": "REJECTED"})
    assert rejected.json()["adjustment"] == {"status": "REQUIRED", "amount_minor": 41000 - 20500}
    assert razorpay_net(20500) + razorpay_net(41000 - 20500) >= 40000

    old_registration, old_member = _acm_member(old_payload["team_name"])
    old_rejected = harness.client.post(f"/api/v1/admin/teams/{old_registration.id}/members/{old_member.id}/acm-review", headers=admin, json={"status": "REJECTED"})
    assert old_rejected.json()["adjustment"] == {"status": "REQUIRED", "amount_minor": 20000}
    assert harness.client.get("/api/v1/participant/dashboard", headers=old_leader).json()["amount_minor"] == 20000
