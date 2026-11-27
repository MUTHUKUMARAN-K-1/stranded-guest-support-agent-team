import csv
from copy import deepcopy

import pytest

from guest_support.policy import PolicyError, comparable, money_cents, qualifies, safety_conditions
from guest_support.store import ROOT, TICKETS_FILE


def ticket_rows():
    with (ROOT / TICKETS_FILE).open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


@pytest.mark.parametrize("ticket", ticket_rows(), ids=lambda row: row["ticket_id"])
def test_safety_matches_independent_fixture_labels(ticket):
    # Gold labels are test-only. Production code classifies the guest message.
    assert bool(safety_conditions(ticket)) == (ticket["expected_outcome"] == "safety")
    assert qualifies(ticket)


@pytest.mark.parametrize(
    "message,condition",
    [
        ("My daughter is pregnant and needs medication", "injury_or_medical_need"),
        ("The host threatened us", "threat_or_violence"),
        ("We are out on the street at night", "nowhere_safe_to_wait"),
        ("I am 17 and waiting alone", "unaccompanied_minor"),
        ("I am afraid of the host", "stated_fear_of_person"),
    ],
)
def test_each_of_five_safety_conditions(message, condition):
    assert condition in safety_conditions({"guest_message": message})


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "1.001", "oops", "1e1000"])
def test_invalid_money_is_rejected(amount):
    with pytest.raises(PolicyError, match="INVALID_AMOUNT"):
        money_cents(amount)


def base_comparable():
    ticket = {
        "check_in_date": "2026-11-16",
        "check_out_date": "2026-11-20",
        "party_size": 4,
        "bedrooms": 2,
        "nightly_price": "110.00",
    }
    listing = {
        "available_from": "2026-11-16",
        "available_to": "2026-11-20",
        "status": "available",
        "max_guests": 4,
        "bedrooms": 2,
        "nightly_price": "132.00",
        "neighborhood": "Elsewhere",
    }
    return ticket, listing


def test_exact_price_boundary_and_neighborhood_not_a_filter():
    ticket, listing = base_comparable()
    assert comparable(ticket, listing)
    listing["nightly_price"] = "132.01"
    assert not comparable(ticket, listing)


@pytest.mark.parametrize(
    "field,value",
    [
        ("available_from", "2026-11-17"),
        ("available_to", "2026-11-19"),
        ("max_guests", 3),
        ("bedrooms", 1),
        ("status", "on_hold"),
        ("status", "booked"),
    ],
)
def test_each_comparability_requirement_is_binding(field, value):
    ticket, listing = base_comparable()
    changed = deepcopy(listing)
    changed[field] = value
    assert not comparable(ticket, changed)


def test_eligibility_not_assumed_for_arbitrary_ticket():
    ticket = {
        "check_in_date": "2026-11-16",
        "host_cancelled_at": "2026-09-01T12:00:00Z",
        "host_last_reply_at": "2026-11-16T20:00:00Z",
        "opened_at": "2026-11-16T13:00:00Z",
        "guest_message": "Everything is fine",
    }
    assert not qualifies(ticket)
    ticket["host_cancelled_at"] = "2026-10-17T12:00:00Z"
    assert qualifies(ticket)  # Exact 30-day boundary.
