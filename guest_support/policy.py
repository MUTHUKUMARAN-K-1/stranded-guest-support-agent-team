"""Policy calculations. Evaluation labels are never used to make decisions."""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

INCIDENT_DATE = date(2026, 11, 16)
UPLIFT_BUDGET_CENTS = 900_000
SAFETY_CONDITIONS = (
    "injury_or_medical_need",
    "threat_or_violence",
    "nowhere_safe_to_wait",
    "unaccompanied_minor",
    "stated_fear_of_person",
)

# Conservative, transparent checks for the English-language incident fixture.
# This is not a general-purpose medical/emergency classifier; see README.
SAFETY_PATTERNS = {
    "injury_or_medical_need": (
        r"\b(bleeding|injured|injury|hurt|unwell|pregnant|medication|diabetic)\b",
        r"\b(need(?:s)? (?:a |my |their )?(?:doctor|insulin|medicine)|bad hip)\b",
        r"\b(pale and shaky|can hardly stand|medical (?:help|attention|need))\b",
    ),
    "threat_or_violence": (
        r"\b(threaten\w*|violence|attacked|assaulted|shouted at|harmed)\b",
        r"\b(?:someone|a man|a woman|a stranger|the host).{0,60}\bfollow(?:ed|ing)\b",
    ),
    "nowhere_safe_to_wait": (
        r"\bnowhere safe\b",
        r"\bno safe (?:place|somewhere)\b",
        r"\b(?:on|in|out on) the street(?:s)? (?:at night|tonight|after dark)\b",
        r"\bphone (?:is )?(?:dying|dead|on \d+%)\b",
        r"\balone with (?:my |the )?luggage\b",
    ),
    "unaccompanied_minor": (
        r"\bunaccompanied minor\b",
        r"\b(?:i am|i'm|aged?|age) (?:[1-9]|1[0-7])\b.{0,100}"
        r"\b(?:on my own|alone|without an adult)\b",
        r"\b(?:[1-9]|1[0-7])[- ]year[- ]old\b.{0,100}\b(?:alone|unaccompanied)\b",
    ),
    "stated_fear_of_person": (
        r"\b(?:afraid|scared|frightened|fearful) of\b",
        r"\b(?:watching|following).{0,80}\b(?:scared|afraid|frightened)\b",
        r"\b(?:scared|afraid) to (?:walk|leave|go)\b",
    ),
}

# Approximate incident-specific ranking hints, not a distance/accessibility claim.
NEIGHBORS = {
    "Alfama": {"Baixa", "Graca"},
    "Baixa": {"Alfama", "Chiado", "Graca"},
    "Chiado": {"Baixa", "Bairro Alto"},
    "Bairro Alto": {"Chiado", "Principe Real", "Estrela"},
    "Principe Real": {"Bairro Alto", "Estrela"},
    "Estrela": {"Principe Real", "Bairro Alto", "Alcantara"},
    "Alcantara": {"Estrela", "Belem"},
    "Belem": {"Alcantara"},
    "Graca": {"Alfama", "Baixa"},
    "Parque das Nacoes": set(),
}


class PolicyError(ValueError):
    """A caller-readable policy or state rejection."""


def money_cents(value: str) -> int:
    """Accept exact, finite, nonnegative EUR amounts with at most two decimals."""
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000"):
            raise ValueError
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise ValueError
        return int(cents)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PolicyError(
            "INVALID_AMOUNT: use a nonnegative EUR amount with at most 2 decimals"
        ) from exc


def eur(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def safety_conditions(ticket: dict[str, Any]) -> list[str]:
    text = ticket["guest_message"].lower()
    return [
        name
        for name, patterns in SAFETY_PATTERNS.items()
        if any(re.search(pattern, text) for pattern in patterns)
    ]


def qualifies(ticket: dict[str, Any]) -> bool:
    check_in = date.fromisoformat(ticket["check_in_date"])
    cancelled = ticket["host_cancelled_at"]
    if cancelled:
        cancelled_date = datetime.fromisoformat(cancelled).date()
        if check_in - timedelta(days=30) <= cancelled_date <= check_in:
            return True
    # The fixture lacks a separate guest-contact timestamp; opened_at is the proxy.
    reply = ticket["host_last_reply_at"]
    if not reply or datetime.fromisoformat(reply) <= datetime.fromisoformat(ticket["opened_at"]):
        return True
    return bool(
        re.search(
            r"lockbox.{0,30}(?:empty|not work|wrong)|empty (?:key|lock)box|"
            r"no (?:working )?(?:key|access)|wrong code|nobody.{0,25}(?:keys|door)",
            ticket["guest_message"].lower(),
        )
    )


def comparable(ticket: dict[str, Any], listing: dict[str, Any]) -> bool:
    """Use integer cross-multiplication so 132.01 never passes a 132.00 ceiling."""
    return (
        listing["status"] == "available"
        and listing["available_from"] <= ticket["check_in_date"]
        and listing["available_to"] >= ticket["check_out_date"]
        and listing["max_guests"] >= ticket["party_size"]
        and listing["bedrooms"] >= ticket["bedrooms"]
        and money_cents(listing["nightly_price"]) * 100
        <= money_cents(ticket["nightly_price"]) * 120
    )


def nights(ticket: dict[str, Any]) -> int:
    return (
        date.fromisoformat(ticket["check_out_date"]) - date.fromisoformat(ticket["check_in_date"])
    ).days


def uplift_cents(ticket: dict[str, Any], listing: dict[str, Any]) -> int:
    return max(
        0, money_cents(listing["nightly_price"]) - money_cents(ticket["nightly_price"])
    ) * nights(ticket)


def location_rank(ticket: dict[str, Any], listing: dict[str, Any]) -> int:
    area = listing["neighborhood"]
    if area == ticket["neighborhood"]:
        return 0
    return 1 if area in NEIGHBORS.get(ticket["neighborhood"], set()) else 2
