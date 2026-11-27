# Ticket handling rules: Lisbon, 16 November 2026

Derived from the supplied **AirCover Rebooking and Refund Rules - Lisbon Check-In Incident.md**.
This document is the decision flow used to write all six tool descriptions. The source policy
is the authority if this summary differs. CSV names in that policy map to the supplied files
`Lisbon Stranded Guest Tickets (16 Nov 2026).csv` and
`Lisbon Open Listings Export (16 Nov 2026).csv` without changing their contents.

1. **Read safety evidence first.** Injury or medical need, threats or violence, nowhere safe
   to wait, an unaccompanied minor, or stated fear of a person means Trust and Safety.
   Any one flag suffices, including on a refund-only request. Call only
   `handoff_to_safety_agent`; no reservation lookup, search, hold, refund or host messaging.
   After handoff, a live safety agent decides any booking or money action. The local simulator
   must report a queued handoff honestly, never claim a live agent has connected.
2. **Establish check-in failure.** Qualify through missing working access, unreachable host,
   or host cancellation within 30 days of check-in. Cover check-in through check-out, not
   only the first night. In this dataset `opened_at` is the contact-time proxy because no
   separate guest-contact timestamp exists.
3. **Respect the guest's choice.** A guest declining rebooking gets full `total_paid`.
   Rejecting one offer does not cancel support. Callback/other requests are not automatically
   emergencies or refund requests. Do not infer danger merely from anger or figurative speech.
4. **Search all 450 homes.** A comparable home must cover the complete dates, fit the party,
   have at least the original bedrooms, currently be `available`, and cost at most 120% of
   the original nightly price. Prefer the same or neighboring area; other areas still qualify.
   Use actual export IDs, never original-listing IDs (which are absent from the export).
5. **Hold only an accepted, still-valid offer.** Recheck every comparison at hold time.
   Charge positive nightly uplift times the full number of nights against the shared EUR 9000
   incident budget. EUR 150 is a planning average, not a guest cap. A price above 120% cannot
   be offered in this incident: the required policy manager sign-off is unavailable.
6. **Refund within the cap.** Amounts must be exact cents, from EUR 0.01 through `total_paid`.
   Full `total_paid` is mandatory for guest refusal or no comparable homes. Other partial
   refunds within the cap are allowed by the supplied policy. No goodwill additions. Refunds
   close the stay; never combine a refund and rebooking for the same nights.
7. **Use host messaging without changing the remedy.** Look up the host from the original
   reservation or a comparable replacement; messaging neither holds a home nor spends money.
   Safety restrictions still apply. Record messages in the local outbox only.
8. **Handle retries and races.** Return an existing identical hold/refund/handoff rather than
   repeating it. Use a unique `request_id` for each host message. Persist availability,
   remedies, incident uplift and successful-call events atomically in SQLite.

`expected_outcome` is a gold label for tests. It is excluded from the runtime database and
tool responses, and must never decide an agent's action.
