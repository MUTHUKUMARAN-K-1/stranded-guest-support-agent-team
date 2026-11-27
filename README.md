# Stranded guest support agent team: six booking tools

Stage: **Connect the agents to the booking records** (Zero, Airbnb Lisbon scenario).

A Python FastMCP server exposing exactly six booking tools over the supplied **450 Lisbon
listings** and **60 guest tickets** for **16 November 2026**. It uses the supplied AirCover
incident policy and the derived [ticket handling rules](TICKET_RULES.md).

This is a local training simulation, not an Airbnb API integration. Holds, refunds, host
messages and safety handoffs are stored in SQLite. No real payment, message, reservation or
live safety connection is made. A queued safety handoff is not proof of meeting the incident's
"live safety agent before midnight" commitment.

## Run it

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/) are required for the locked workflow.
From this repository:

```powershell
uv sync --locked --extra dev
uv run --locked python server.py
```

The server uses stdio; run it through an MCP client instead of typing into its terminal.
No API keys are needed for this stage. To use an existing Python environment instead:

```powershell
python -m pip install -e ".[dev]"
python -m guest_support.inspect_tools
python -m pytest -q
```

The source CSVs remain unchanged. The database is created lazily on the first tool call at
`runtime/booking.sqlite3` and survives server restarts. All server processes for this incident
must use the same database path to share availability and the uplift budget. To run a fresh
experiment, set `ZERO_DB_PATH` to a new filename; do not overwrite an existing run. Set
`ZERO_DATA_DIR` if the two CSVs live elsewhere. Changed CSV hashes require a new database.

## Connect an MCP client

For this Windows checkout, after `uv sync --locked --extra dev`:

```json
{
  "mcpServers": {
    "lisbon-booking": {
      "command": "D:\\Projects\\zerp\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\zerp\\server.py"]
    }
  }
}
```

Adapt absolute paths for another checkout. The server resolves its CSV paths relative to its
source directory, not the client's current directory. The booking server is the tool layer;
the supervisor/specialist agent team is a later stage.

## Printed tool list

Command (uses the actual MCP `tools/list` response):

```powershell
uv run --locked python -m guest_support.inspect_tools
```

Verified output:

```text
Discovered 6 tools:
- get_reservation
- search_available_homes
- hold_booking
- issue_refund
- message_host
- handoff_to_safety_agent
```

The command also prints every description, input schema and output schema. The captured
response is in [TOOL_CATALOG.txt](TOOL_CATALOG.txt).

| Tool | Purpose and policy constraint |
| --- | --- |
| `get_reservation` | Read the full-stay reservation facts, total paid and current remedy; exclude evaluation labels. |
| `search_available_homes` | Search all source listings against dates, party size, bedrooms, current status and the 120% nightly price ceiling. |
| `hold_booking` | With guest acceptance, recheck comparability and reserve shared uplift atomically; no refund plus hold. |
| `issue_refund` | Exact EUR cents up to `total_paid`; full amount for refusal or no comparable home; close the stay. |
| `message_host` | Resolve the original or suitable replacement host; write to a local outbox with retry protection. |
| `handoff_to_safety_agent` | Queue any of five safety conditions for Trust and Safety; block all other tools before and after handoff. |

## Example calls

The same calls work through any MCP client connected to this server. Start with fresh
state for each independent example:

```text
get_reservation(reservation_id="RES-2026-10001")
search_available_homes(reservation_id="RES-2026-10001", limit=10)

# Use a listing_id returned by search; do not invent an ID or guest acceptance.
hold_booking(reservation_id="RES-2026-10001", listing_id="<returned ID>", guest_accepted=true)

issue_refund(reservation_id="RES-2026-10004", amount_eur="168.00",
             reason="guest_declined_rebooking")

message_host(reservation_id="RES-2026-10003", message="Please confirm working key access.",
             request_id="access-check-1")

# This ticket reports an injury even though the guest asks for a refund.
handoff_to_safety_agent(reservation_id="RES-2026-10007",
                        conditions=["injury_or_medical_need"],
                        note="Guest reports a bleeding knee and can hardly stand.")
```

Agents receive ticket context from the scenario. They must read its safety evidence before
calling any other tool. Server-side checks independently reject known safety conditions in
the stored ticket, so a wrong agent tool selection does not bypass those checks.

## Key Claude Code instruction excerpt

The following is a **reusable Claude Code implementation prompt**, included for the stage's
deliverable. This implementation was produced with Codex; it is not a claimed Claude Code
execution transcript.

> Build exactly six FastMCP booking tools for the Lisbon stranded-guest incident:
> get_reservation, search_available_homes, hold_booking, issue_refund, message_host and
> handoff_to_safety_agent. Use the supplied 450-row listings CSV and 60-row tickets CSV,
> and derive each plain-language tool description from TICKET_RULES.md and the AirCover
> policy. Read safety evidence first: any of the five conditions permits only a safety
> handoff, including refund-only tickets. Search the whole stay with all five comparability
> checks and a hard 120% price ceiling. Enforce exact-cent refunds up to total_paid, full
> refunds for refusal/no match, one remedy per stay and a shared EUR 9000 uplift budget.
> EUR 150 is an average, not a per-guest limit. Persist simulated mutations atomically,
> make retries safe, and never claim live Airbnb actions. Exclude expected_outcome labels
> from runtime decisions and responses. Print the actual MCP tool list and test policy
> boundaries, safety overrides, duplicate calls and concurrent availability changes.

## Tool description compared with the rules

`issue_refund` says full `total_paid` is due if the guest declines rebooking or no comparable
home exists, and explicitly says **safety overrides a refund request**. This maps to rules
1, 3 and 6 above, rather than treating `guest_requested=refund` as permission to pay.

Concrete check: ticket `LIS-TKT-0007` requests a refund but describes a bleeding knee and
inability to stand. `issue_refund` rejects it with `SAFETY_HANDOFF_REQUIRED`; only
`handoff_to_safety_agent` succeeds. The block remains after the handoff. Ticket
`LIS-TKT-0004`, which has a non-safety refusal, accepts a full EUR 168.00 refund and rejects
a partial EUR 1.00 refund. Thus the description is backed by executable checks, not only
prompt wording.

## What was checked or changed

Final verification in `D:\Projects\zerp`: **105 tests passed**, Ruff lint passed, and Ruff
format checks passed. The original two CSVs and supplied policy remained byte-for-byte unchanged.

- Imported exactly 450 listings and 60 tickets. Fixed handling of blank listing ratings:
  preserve them as unrated, without excluding otherwise comparable homes.
- Excluded `expected_outcome` from the runtime database and reservation responses; used
  those labels only for the independent 60-ticket safety regression test.
- Checked each comparability condition, full-stay date coverage, EUR 132.00 versus EUR 132.01
  at an original EUR 110.00 rate, and unavailable listings changing between search and hold.
- Checked all six supplied safety cases, all five condition types, refund-only safety cases,
  and continued blocking after handoff. Figurative "I am dying out here" alone does not
  trigger a medical emergency in the supplied fixture.
- Checked refund floors/caps, mandatory full refunds, refusal rights, partial refunds where
  permitted, one remedy per stay, idempotent retries, persistent holds and local host routing.
- Checked the EUR 9000 budget, accepted uplift above EUR 150 when within policy, atomic
  rollback on budget failure, two concurrent attempts to hold the same home, and concurrent
  holds of different homes competing for the same incident budget.
- Exercised all six tools through an in-memory FastMCP client and discovered the same six
  through the executable stdio entry point. Captured the actual tool catalog.

Run checks:

```powershell
uv run --locked python -m pytest -q
uv run --locked ruff check guest_support tests server.py
uv run --locked ruff format --check guest_support tests server.py
```

## Scope and assumptions

- Safety detection is transparent, conservative English pattern matching for this incident
  fixture. It matches the supplied six safety tickets, but is not a general natural-language
  safety guarantee. Unseen wording, negation and other languages need reviewed classification
  and human escalation before any production use. Agents can explicitly hand off newly
  observed conditions, which then block all other tools.
- `guest_accepted` and a newly reported refusal are caller assertions for the exercise; a real
  integration would need authenticated guest consent. Initial refund requests cannot be
  silently changed into rebookings by setting a boolean.
- Neighborhood preference uses a small explicit approximate adjacency table; the export
  has no distances. Area ranking is a preference only. Accessibility, lifts, cots, parking
  and other amenities are not invented from listing titles or absent columns.
- A hold reserves the entire listing and uplift in the local ledger without expiring. It is
  conservative, not a real booking confirmation. Confirmation, hold release/expiry, real
  payouts, host delivery and staffed safety queues are outside this six-tool stage.
- The incident date is fixed to 16 November 2026; audit timestamps show execution time.
  There is no simulated midnight SLA guarantee or live monitoring.

Implementation references: [FastMCP tools](https://gofastmcp.com/servers/tools) and
[FastMCP client/testing](https://gofastmcp.com/clients/client). Dependencies are locked in
`uv.lock`; verification uses FastMCP 3.4.11 on Python 3.11.
