import csv
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

import guest_support.store as store_module
from guest_support.policy import PolicyError
from guest_support.store import LISTINGS_FILE, ROOT, BookingStore

RESERVATION = "RES-2026-10001"


def first_home(store, reservation=RESERVATION):
    return store.search_available_homes(reservation, 50)["homes"][0]


def test_csv_loaded_without_evaluation_leakage(store):
    with store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 450
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 60
        assert all(
            "expected_outcome" not in row[0] for row in db.execute("SELECT payload FROM tickets")
        )
    reservation = store.get_reservation(RESERVATION)
    assert reservation["reservation"]["original_listing_id"] == "LST-200001"
    assert reservation["nights"] == 3


def test_search_all_csv_matches_using_independent_decimal_checks(store):
    result = store.search_available_homes(RESERVATION, 50)
    with (ROOT / LISTINGS_FILE).open(encoding="utf-8-sig", newline="") as file:
        expected = {
            row["listing_id"]
            for row in csv.DictReader(file)
            if row["status"] == "available"
            and row["available_from"] <= "2026-11-16"
            and row["available_to"] >= "2026-11-19"
            and int(row["max_guests"]) >= 2
            and int(row["bedrooms"]) >= 0
            and Decimal(row["nightly_price"]) <= Decimal("110.40")
        }
    assert result["total_comparable"] == len(expected)
    assert {home["listing_id"] for home in result["homes"]} <= expected
    assert len(result["homes"]) == min(50, len(expected))
    assert result["price_ceiling_eur"] == "110.40"


def test_hold_persists_and_retries_do_not_double_spend(store):
    home = first_home(store)
    result = store.hold_booking(RESERVATION, home["listing_id"], True)
    assert result["check_out_date"] == "2026-11-19"
    assert result["simulated"] is True
    restarted = BookingStore(db_path=store.db_path)
    assert restarted.hold_booking(RESERVATION, home["listing_id"], True) == result
    with store.transaction() as db:
        assert (
            db.execute("SELECT status FROM listings WHERE id=?", (home["listing_id"],)).fetchone()[
                0
            ]
            == "on_hold"
        )
        assert (
            db.execute("SELECT COUNT(*) FROM events WHERE tool='hold_booking'").fetchone()[0] == 1
        )
    with pytest.raises(PolicyError, match="STAY_CLOSED"):
        store.issue_refund(RESERVATION, "276.00", "guest_declined_rebooking")


def test_refund_requested_means_full_amount_and_closes_stay(store):
    reservation = "RES-2026-10004"
    with pytest.raises(PolicyError, match="FULL_REFUND_DUE"):
        store.issue_refund(reservation, "1.00", "partial_refund")
    result = store.issue_refund(reservation, "168.00", "guest_declined_rebooking")
    assert store.issue_refund(reservation, "168.00", "guest_declined_rebooking") == result
    with pytest.raises(PolicyError, match="GUEST_DECLINED_REBOOKING|STAY_CLOSED"):
        store.hold_booking(reservation, "LST-100004", True)
    with pytest.raises(PolicyError, match="STAY_CLOSED"):
        store.issue_refund(reservation, "1.00", "partial_refund")


@pytest.mark.parametrize("amount", ["0", "276.01", "99999"])
def test_refund_floor_and_total_paid_cap(store, amount):
    with pytest.raises(PolicyError, match="REFUND_CAP"):
        store.issue_refund(RESERVATION, amount, "partial_refund")


def test_policy_allows_partial_where_full_not_due(store):
    assert store.issue_refund(RESERVATION, "0.01", "partial_refund")["amount_eur"] == "0.01"
    with pytest.raises(PolicyError, match="STAY_CLOSED"):
        store.search_available_homes(RESERVATION)


def test_no_home_full_refund_and_refusal_right(store):
    with pytest.raises(PolicyError, match="COMPARABLE_HOME_EXISTS"):
        store.issue_refund(RESERVATION, "276.00", "no_comparable_home")
    # Declining an offer does not require pretending no comparable home exists.
    result = store.issue_refund(RESERVATION, "276.00", "guest_declined_rebooking")
    assert result["amount_eur"] == "276.00"


def test_no_comparable_means_full_refund(store):
    with store.transaction() as db:
        db.execute("UPDATE listings SET status='booked'")
    assert store.search_available_homes(RESERVATION)["full_refund_due_if_no_comparable"]
    with pytest.raises(PolicyError, match="FULL_REFUND_DUE"):
        store.issue_refund(RESERVATION, "1.00", "partial_refund")
    assert store.issue_refund(RESERVATION, "276.00", "no_comparable_home")["status"] == "refunded"


@pytest.mark.parametrize(
    "reservation",
    [
        "RES-2026-10007",
        "RES-2026-10015",
        "RES-2026-10023",
        "RES-2026-10034",
        "RES-2026-10046",
        "RES-2026-10055",
    ],
)
def test_safety_blocks_all_five_other_tools_before_and_after_handoff(store, reservation):
    def blocked_calls():
        return [
            lambda: store.get_reservation(reservation),
            lambda: store.search_available_homes(reservation),
            lambda: store.hold_booking(reservation, "LST-100004", True),
            lambda: store.issue_refund(reservation, "1.00", "partial_refund"),
            lambda: store.message_host(reservation, "Hello", "request-1"),
        ]

    for call in blocked_calls():
        with pytest.raises(PolicyError, match="SAFETY_HANDOFF_REQUIRED"):
            call()
    result = store.handoff_to_safety_agent(
        reservation, ["nowhere_safe_to_wait"], "Read the guest message; human assistance needed."
    )
    assert result["live_agent_connected"] is False
    assert result["status"] == "queued_locally"
    assert store.handoff_to_safety_agent(reservation, ["nowhere_safe_to_wait"], "Retry") == result
    for call in blocked_calls():
        with pytest.raises(PolicyError, match="SAFETY_HANDOFF_REQUIRED"):
            call()


def test_explicit_handoff_also_blocks_previously_unflagged_ticket(store):
    store.handoff_to_safety_agent(
        RESERVATION, ["stated_fear_of_person"], "New information received by the agent."
    )
    with pytest.raises(PolicyError, match="SAFETY_HANDOFF_REQUIRED"):
        store.issue_refund(RESERVATION, "276.00", "guest_declined_rebooking")


def test_current_status_is_rechecked_at_hold_time(store):
    home = first_home(store)
    with store.transaction() as db:
        db.execute("UPDATE listings SET status='booked' WHERE id=?", (home["listing_id"],))
    with pytest.raises(PolicyError, match="NOT_COMPARABLE"):
        store.hold_booking(RESERVATION, home["listing_id"], True)


def test_original_listing_and_unaccepted_offer_cannot_be_held(store):
    with pytest.raises(PolicyError, match="LISTING_NOT_FOUND"):
        store.hold_booking(RESERVATION, "LST-200001", True)
    with pytest.raises(PolicyError, match="GUEST_CONSENT_REQUIRED"):
        store.hold_booking(RESERVATION, first_home(store)["listing_id"], False)


def test_uplift_budget_and_150_is_not_a_guest_cap(store, monkeypatch):
    selected = None
    for number in (9, 31, 35, 33, 13):
        reservation = f"RES-2026-{10000 + number}"
        homes = store.search_available_homes(reservation, 50)["homes"]
        selected = next(
            ((reservation, home) for home in homes if Decimal(home["uplift_eur"]) > 150), None
        )
        if selected:
            break
    assert selected is not None
    reservation, home = selected
    monkeypatch.setattr(store_module, "UPLIFT_BUDGET_CENTS", 1)
    with pytest.raises(PolicyError, match="UPLIFT_BUDGET_EXCEEDED"):
        store.hold_booking(reservation, home["listing_id"], True)
    with store.transaction() as db:
        assert db.execute("SELECT SUM(uplift_cents) FROM cases").fetchone()[0] == 0
    monkeypatch.setattr(store_module, "UPLIFT_BUDGET_CENTS", 900_000)
    assert Decimal(store.hold_booking(reservation, home["listing_id"], True)["uplift_eur"]) > 150


def test_concurrent_claims_cannot_hold_same_home(store):
    home = first_home(store)
    # Give another non-safety case identical booking requirements in this isolated DB.
    with store.transaction() as db:
        ticket = store._ticket(db, RESERVATION)
        ticket["reservation_id"] = "RES-2026-10002"
        ticket["ticket_id"] = "LIS-TKT-0002"
        db.execute(
            "UPDATE tickets SET payload=? WHERE reservation_id=?",
            (json.dumps(ticket), ticket["reservation_id"]),
        )

    def attempt(reservation):
        try:
            return store.hold_booking(reservation, home["listing_id"], True)["status"]
        except PolicyError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [RESERVATION, "RES-2026-10002"]))
    assert results.count("on_hold") == 1
    assert sum(result.startswith("NOT_COMPARABLE") for result in results) == 1


def test_concurrent_different_homes_share_one_incident_budget(store, monkeypatch):
    choices = []
    for reservation in ("RES-2026-10009", "RES-2026-10031"):
        home = next(
            home
            for home in store.search_available_homes(reservation, 50)["homes"]
            if Decimal(home["uplift_eur"]) > 0
            and home["listing_id"] not in {choice[1]["listing_id"] for choice in choices}
        )
        choices.append((reservation, home))
    budget = max(int(Decimal(home["uplift_eur"]) * 100) for _, home in choices)
    monkeypatch.setattr(store_module, "UPLIFT_BUDGET_CENTS", budget)

    def attempt(choice):
        reservation, home = choice
        try:
            return store.hold_booking(reservation, home["listing_id"], True)["status"]
        except PolicyError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, choices))
    assert results.count("on_hold") == 1
    assert sum(result.startswith("UPLIFT_BUDGET_EXCEEDED") for result in results) == 1
    with store.transaction() as db:
        assert db.execute("SELECT SUM(uplift_cents) FROM cases").fetchone()[0] <= budget


def test_host_message_resolves_host_and_is_idempotent(store):
    result = store.message_host(RESERVATION, "Please confirm key access.", "request-1")
    assert result["host_id"] == "HOST-4001"
    assert result["status"] == "recorded_locally"
    assert store.message_host(RESERVATION, "Please confirm key access.", "request-1") == result
    with pytest.raises(PolicyError, match="REQUEST_ID_CONFLICT"):
        store.message_host(RESERVATION, "Different message", "request-1")
    home = first_home(store)
    replacement = store.message_host(
        RESERVATION, "Can you receive these guests?", "request-2", home["listing_id"]
    )
    assert replacement["host_id"] == home["host_id"]


def test_unknown_reservation_fails(store):
    with pytest.raises(PolicyError, match="RESERVATION_NOT_FOUND"):
        store.get_reservation("RES-2026-99999")
