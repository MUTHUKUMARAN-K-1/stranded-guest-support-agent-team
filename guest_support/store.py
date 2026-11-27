"""SQLite-backed training records; all writes are transactional and local."""

import csv
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .policy import (
    INCIDENT_DATE,
    SAFETY_CONDITIONS,
    UPLIFT_BUDGET_CENTS,
    PolicyError,
    comparable,
    eur,
    location_rank,
    money_cents,
    nights,
    qualifies,
    safety_conditions,
    uplift_cents,
)

LISTINGS_FILE = "Lisbon Open Listings Export (16 Nov 2026).csv"
TICKETS_FILE = "Lisbon Stranded Guest Tickets (16 Nov 2026).csv"
ROOT = Path(__file__).resolve().parent.parent


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BookingStore:
    def __init__(self, data_dir: Path = ROOT, db_path: Path = ROOT / "runtime" / "booking.sqlite3"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir = Path(data_dir)
        self._initialize()

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self):
        sources = [self.data_dir / LISTINGS_FILE, self.data_dir / TICKETS_FILE]
        digest = hashlib.sha256(b"".join(path.read_bytes() for path in sources)).hexdigest()
        with self.transaction() as db:
            for statement in (
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS listings (id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                "status TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, "
                "reservation_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS cases (reservation_id TEXT PRIMARY KEY "
                "REFERENCES tickets(reservation_id), state TEXT NOT NULL DEFAULT 'open', "
                "listing_id TEXT, uplift_cents INTEGER NOT NULL DEFAULT 0, "
                "remedy TEXT, handoff TEXT)",
                "CREATE TABLE IF NOT EXISTS messages (reservation_id TEXT NOT NULL "
                "REFERENCES tickets(reservation_id), request_id TEXT NOT NULL, "
                "payload TEXT NOT NULL, "
                "PRIMARY KEY(reservation_id, request_id))",
                "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, "
                "timestamp TEXT NOT NULL, "
                "tool TEXT NOT NULL, reservation_id TEXT NOT NULL, payload TEXT NOT NULL)",
            ):
                db.execute(statement)
            prior = db.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchone()
            if prior:
                if prior[0] != digest:
                    raise PolicyError(
                        "SOURCE_CHANGED: choose a new database path for changed CSV files"
                    )
                return
            with sources[0].open(encoding="utf-8-sig", newline="") as file:
                listings = list(csv.DictReader(file))
            with sources[1].open(encoding="utf-8-sig", newline="") as file:
                tickets = list(csv.DictReader(file))
            if len(listings) != 450 or len(tickets) != 60:
                raise PolicyError("INVALID_FIXTURE: expected 450 listings and 60 tickets")
            for row in listings:
                row["bedrooms"] = int(row["bedrooms"])
                row["max_guests"] = int(row["max_guests"])
                row["nightly_price"] = eur(money_cents(row["nightly_price"]))
                row["instant_book"] = row["instant_book"].lower() == "true"
                row["rating"] = float(row["rating"]) if row["rating"].strip() else None
                for field in ("available_from", "available_to"):
                    date.fromisoformat(row[field])
                if row["status"] not in {"available", "on_hold", "booked"}:
                    raise PolicyError("INVALID_LISTING_STATUS")
                db.execute(
                    "INSERT INTO listings VALUES (?, ?, ?)",
                    (row["listing_id"], json.dumps(row), row["status"]),
                )
            for row in tickets:
                # Keep the evaluation-only column entirely out of the runtime database.
                row.pop("expected_outcome", None)
                for field in ("bedrooms", "party_size"):
                    row[field] = int(row[field])
                for field in ("nightly_price", "total_paid"):
                    row[field] = eur(money_cents(row[field]))
                if date.fromisoformat(row["check_in_date"]) != INCIDENT_DATE or nights(row) <= 0:
                    raise PolicyError("INVALID_INCIDENT_DATES")
                db.execute(
                    "INSERT INTO tickets VALUES (?, ?, ?)",
                    (row["ticket_id"], row["reservation_id"], json.dumps(row)),
                )
                db.execute("INSERT INTO cases(reservation_id) VALUES (?)", (row["reservation_id"],))
            db.execute("INSERT INTO metadata VALUES ('source_sha256', ?)", (digest,))

    @staticmethod
    def _ticket(db, reservation_id):
        row = db.execute(
            "SELECT payload FROM tickets WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if not row:
            raise PolicyError("RESERVATION_NOT_FOUND: use a reservation_id from the ticket records")
        return json.loads(row[0])

    @staticmethod
    def _case(db, reservation_id):
        return dict(
            db.execute("SELECT * FROM cases WHERE reservation_id=?", (reservation_id,)).fetchone()
        )

    def _guard(self, db, ticket):
        conditions = safety_conditions(ticket)
        case = self._case(db, ticket["reservation_id"])
        if conditions or case["handoff"]:
            raise PolicyError(
                "SAFETY_HANDOFF_REQUIRED: only handoff_to_safety_agent is allowed; "
                f"conditions={conditions or ['existing_handoff']}"
            )
        if not qualifies(ticket):
            raise PolicyError("NOT_ELIGIBLE: no supported check-in failure found")
        return case

    @staticmethod
    def _open(case):
        if case["state"] != "open":
            raise PolicyError("STAY_CLOSED: one remedy per stay; no refund plus rebooking")

    @staticmethod
    def _listings(db):
        result = []
        for row in db.execute("SELECT payload, status FROM listings"):
            listing = json.loads(row[0])
            listing["status"] = row[1]
            result.append(listing)
        return result

    def _matches(self, db, ticket):
        matches = [listing for listing in self._listings(db) if comparable(ticket, listing)]
        return sorted(
            matches,
            key=lambda listing: (
                location_rank(ticket, listing),
                uplift_cents(ticket, listing),
                money_cents(listing["nightly_price"]),
                -(listing["rating"] or 0),
                listing["listing_id"],
            ),
        )

    @staticmethod
    def _remaining(db):
        used = db.execute("SELECT COALESCE(SUM(uplift_cents), 0) FROM cases").fetchone()[0]
        return UPLIFT_BUDGET_CENTS - used

    @staticmethod
    def _event(db, tool, reservation_id, payload):
        db.execute(
            "INSERT INTO events(timestamp, tool, reservation_id, payload) VALUES (?, ?, ?, ?)",
            (now(), tool, reservation_id, json.dumps(payload)),
        )

    def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            case = self._guard(db, ticket)
            result = {
                "reservation": ticket,
                "case_state": case["state"],
                "nights": nights(ticket),
                "eligible": True,
                "remedy": json.loads(case["remedy"]) if case["remedy"] else None,
            }
            self._event(db, "get_reservation", reservation_id, {"state": case["state"]})
            return result

    def search_available_homes(self, reservation_id: str, limit: int = 10) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise PolicyError("INVALID_LIMIT: use an integer from 1 to 50")
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            self._open(self._guard(db, ticket))
            matches = self._matches(db, ticket)
            remaining = self._remaining(db)
            homes = [
                {
                    **listing,
                    "uplift_eur": eur(uplift_cents(ticket, listing)),
                    "location_preference_rank": location_rank(ticket, listing),
                    "within_remaining_uplift_budget": uplift_cents(ticket, listing) <= remaining,
                }
                for listing in matches[:limit]
            ]
            result = {
                "reservation_id": reservation_id,
                "check_in_date": ticket["check_in_date"],
                "check_out_date": ticket["check_out_date"],
                "nights": nights(ticket),
                "price_ceiling_eur": eur(money_cents(ticket["nightly_price"]) * 120 // 100),
                "total_comparable": len(matches),
                "homes": homes,
                "remaining_uplift_budget_eur": eur(remaining),
                "full_refund_due_if_no_comparable": not matches,
            }
            self._event(
                db, "search_available_homes", reservation_id, {"total_comparable": len(matches)}
            )
            return result

    def hold_booking(
        self, reservation_id: str, listing_id: str, guest_accepted: bool
    ) -> dict[str, Any]:
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            case = self._guard(db, ticket)
            if guest_accepted is not True:
                raise PolicyError("GUEST_CONSENT_REQUIRED: an offer is not a guest acceptance")
            if ticket["guest_requested"] == "refund":
                raise PolicyError("GUEST_DECLINED_REBOOKING: issue the full refund instead")
            if case["state"] == "held" and case["listing_id"] == listing_id:
                return json.loads(case["remedy"])
            self._open(case)
            listing = next(
                (home for home in self._listings(db) if home["listing_id"] == listing_id), None
            )
            if not listing:
                raise PolicyError(
                    "LISTING_NOT_FOUND: use an ID from the 450-row open listings export"
                )
            if not comparable(ticket, listing):
                raise PolicyError(
                    "NOT_COMPARABLE: full dates, guests, bedrooms, available status "
                    "and nightly price <=120% must ALL pass"
                )
            uplift = uplift_cents(ticket, listing)
            if uplift > self._remaining(db):
                raise PolicyError("UPLIFT_BUDGET_EXCEEDED: the incident cap is EUR 9000")
            result = {
                "hold_id": f"HOLD-{uuid4().hex[:12]}",
                "reservation_id": reservation_id,
                "listing_id": listing_id,
                "status": "on_hold",
                "simulated": True,
                "check_in_date": ticket["check_in_date"],
                "check_out_date": ticket["check_out_date"],
                "nights": nights(ticket),
                "nightly_price_eur": listing["nightly_price"],
                "uplift_eur": eur(uplift),
                "created_at": now(),
            }
            db.execute("UPDATE listings SET status='on_hold' WHERE id=?", (listing_id,))
            db.execute(
                "UPDATE cases SET state='held', listing_id=?, uplift_cents=?, remedy=? "
                "WHERE reservation_id=?",
                (listing_id, uplift, json.dumps(result), reservation_id),
            )
            self._event(db, "hold_booking", reservation_id, result)
            return result

    def issue_refund(self, reservation_id: str, amount_eur: str, reason: str) -> dict[str, Any]:
        if reason not in {"guest_declined_rebooking", "no_comparable_home", "partial_refund"}:
            raise PolicyError("INVALID_REFUND_REASON")
        amount = money_cents(amount_eur)
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            case = self._guard(db, ticket)
            paid = money_cents(ticket["total_paid"])
            if not 1 <= amount <= paid:
                raise PolicyError(
                    "REFUND_CAP: amount must be EUR 0.01 through total_paid; no goodwill"
                )
            if case["state"] == "refunded":
                prior = json.loads(case["remedy"])
                if prior["amount_eur"] == eur(amount) and prior["reason"] == reason:
                    return prior
            self._open(case)
            matches = self._matches(db, ticket)
            if reason == "no_comparable_home" and matches:
                raise PolicyError("COMPARABLE_HOME_EXISTS: do not claim none are available")
            # A new refusal can be recorded by choosing guest_declined_rebooking.
            full_due = (
                ticket["guest_requested"] == "refund"
                or not matches
                or reason == "guest_declined_rebooking"
            )
            if full_due and amount != paid:
                raise PolicyError("FULL_REFUND_DUE: refund the entire total_paid")
            result = {
                "refund_id": f"REFUND-{uuid4().hex[:12]}",
                "reservation_id": reservation_id,
                "amount_eur": eur(amount),
                "currency": "EUR",
                "reason": reason,
                "status": "refunded",
                "simulated": True,
                "created_at": now(),
            }
            db.execute(
                "UPDATE cases SET state='refunded', remedy=? WHERE reservation_id=?",
                (json.dumps(result), reservation_id),
            )
            self._event(db, "issue_refund", reservation_id, result)
            return result

    def message_host(
        self, reservation_id: str, message: str, request_id: str, listing_id: str | None = None
    ) -> dict[str, Any]:
        if (
            not message.strip()
            or len(message) > 4000
            or not request_id.strip()
            or len(request_id) > 100
        ):
            raise PolicyError(
                "INVALID_MESSAGE: nonempty message <=4000 and request_id <=100 required"
            )
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            case = self._guard(db, ticket)
            if case["state"] == "refunded":
                raise PolicyError("STAY_CLOSED: the stay has been refunded")
            host_id = ticket["host_id"]
            if listing_id:
                listing = next(
                    (home for home in self._listings(db) if home["listing_id"] == listing_id), None
                )
                if not listing:
                    raise PolicyError("LISTING_NOT_FOUND")
                own_hold = case["state"] == "held" and case["listing_id"] == listing_id
                if not own_hold and not comparable(ticket, listing):
                    raise PolicyError("NOT_COMPARABLE: do not contact hosts for unsuitable offers")
                if case["state"] == "held" and not own_hold:
                    raise PolicyError(
                        "STAY_CLOSED: do not pursue another listing after an accepted hold"
                    )
                host_id = listing["host_id"]
            prior_row = db.execute(
                "SELECT payload FROM messages WHERE reservation_id=? AND request_id=?",
                (reservation_id, request_id),
            ).fetchone()
            if prior_row:
                prior = json.loads(prior_row[0])
                if (prior["message"], prior["listing_id"]) != (message, listing_id):
                    raise PolicyError("REQUEST_ID_CONFLICT: reuse an ID only for the same message")
                return prior
            result = {
                "message_id": f"MSG-{uuid4().hex[:12]}",
                "reservation_id": reservation_id,
                "host_id": host_id,
                "listing_id": listing_id,
                "message": message,
                "request_id": request_id,
                "status": "recorded_locally",
                "simulated": True,
                "created_at": now(),
            }
            db.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (reservation_id, request_id, json.dumps(result)),
            )
            self._event(db, "message_host", reservation_id, result)
            return result

    def handoff_to_safety_agent(
        self, reservation_id: str, conditions: list[str], note: str
    ) -> dict[str, Any]:
        if not conditions or any(condition not in SAFETY_CONDITIONS for condition in conditions):
            raise PolicyError(
                "INVALID_SAFETY_CONDITION: choose one or more of the five policy flags"
            )
        if not note.strip() or len(note) > 4000:
            raise PolicyError(
                "INVALID_HANDOFF_NOTE: include a nonempty note of at most 4000 characters"
            )
        with self.transaction() as db:
            ticket = self._ticket(db, reservation_id)
            case = self._case(db, reservation_id)
            if case["handoff"]:
                return json.loads(case["handoff"])
            all_conditions = sorted(set(conditions) | set(safety_conditions(ticket)))
            result = {
                "handoff_id": f"SAFETY-{uuid4().hex[:12]}",
                "reservation_id": reservation_id,
                "ticket_id": ticket["ticket_id"],
                "queue": "Trust and Safety",
                "conditions": all_conditions,
                "note": note,
                "guest_message": ticket["guest_message"],
                "existing_remedy": json.loads(case["remedy"]) if case["remedy"] else None,
                "status": "queued_locally",
                "live_agent_connected": False,
                "simulated": True,
                "created_at": now(),
            }
            db.execute(
                "UPDATE cases SET state='safety_handoff', handoff=? WHERE reservation_id=?",
                (json.dumps(result), reservation_id),
            )
            self._event(db, "handoff_to_safety_agent", reservation_id, result)
            return result
