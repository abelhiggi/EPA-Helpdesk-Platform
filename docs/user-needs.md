# User needs

Three stakeholders, gathered from how the existing shared-mailbox process
actually works. Written before the build; the pytest case names below map
directly onto the acceptance criteria.

## Personas

**Priya, council officer.** Raises a request when something stops working.
Wants to know it arrived and that someone owns it. Does not know or care which
IT team handles what.

**Dan, IT support team lead.** Receives requests. Today he reads every mail in
a shared mailbox and forwards it to the right team, which costs him roughly an
hour a day and means anything arriving after 4pm waits until morning.

**Sam, DDaT service owner.** Accountable for the service. Needs to know where
load falls, whether the AI triage can be trusted, and how long people wait.

## User stories

### MUST — the service does not exist without these

**M1.** As Priya, I want to submit a request with a description so that IT
knows what has gone wrong.
- Authenticated submission returns a reference and a status of `NEW`
- An empty or whitespace-only description is rejected with a clear message
- A description over 4,000 characters is rejected
- `test_valid_ticket_returns_201_with_identifiers`,
  `test_missing_description_is_rejected`,
  `test_oversized_description_is_rejected`

**M2.** As Priya, I want only my own account to be able to raise requests in my
name, so that nobody can submit on my behalf.
- An unauthenticated request is rejected with 401
- The requester is taken from the verified token, and an email supplied in the
  body is ignored
- `test_rejects_request_with_no_verified_claims`,
  `test_requester_comes_from_token_not_body`

**M3.** As Dan, I want each request categorised and assigned to a team
automatically, so that I stop hand-sorting a mailbox.
- Every request reaches `ROUTED` with a category, priority and assigned team
- A category outside the taxonomy falls back rather than being written
- `test_model_result_is_written_to_the_ticket`,
  `test_category_outside_the_taxonomy_falls_back_instead_of_writing_nonsense`

**M4.** As Dan, I want to be emailed when a request is assigned to my team.
- A routed request produces one notification naming category, priority and team
- The notification contains no description and no requester email
- `test_email_contains_no_description_or_requester`

**M5.** As Sam, I want a failed request to be retried and recovered without
anyone watching, so that a transient error is not a lost request.
- A failed request is retried three times before being parked
- A parked request is automatically requeued when the DLQ alarm fires
- `test_first_time_failure_is_requeued`,
  `test_poison_message_is_abandoned_rather_than_looped`

### SHOULD — delivered

**S1.** As Priya, I want to look up my request and see which team has it, so
that I do not have to ring the helpdesk to ask.
- `GET /tickets/{id}` returns status, category, priority and assigned team
- Somebody else's request returns the same 404 as one that does not exist, so
  the endpoint cannot be used to enumerate references
- The response never includes the description I typed
- `test_owner_sees_status_and_routing`,
  `test_another_users_ticket_is_indistinguishable_from_a_missing_one`,
  `test_response_never_returns_the_description`

**S2.** As Dan, I want a priority on every request, so that I know what to pick
up first.
- Priority is one of low, medium, high, and an invalid value falls back
- `test_invalid_priority_falls_back`

**S3.** As Sam, I want to see where load falls, whether triage is accurate, and
how long people wait.
- Dashboard shows tickets by category, mean categorisation confidence, and p90
  time to route
- See `docs/metrics-improvement.md` for the change these metrics drove

**S4.** As Priya, I want the service to be usable with a keyboard and a screen
reader.
- Skip link, visible focus, 44px targets, AA contrast, live regions on results
- Verified with axe DevTools and keyboard-only navigation

### COULD — not built, deliberately

- Ticket history for a requester (needs a second access pattern; the GSI is
  there for it when it is wanted)
- Reopening a closed request
- Teams notification instead of email

### WON'T — this delivery

- Asset management or a CMDB
- Integration with the incumbent ITSM tool
- Anonymous submission (contradicts M2)

## Non-functional needs

| Need | Target | How it is met |
|---|---|---|
| Availability | No single instance to lose | Managed services only, multi-AZ by default; `GET /health` probed every 5 minutes by a CloudWatch Synthetics canary, independent of real traffic, so an outage is detected even when nobody is using the service. Alarms on 2 consecutive breaching periods (~10 min to detect), not 1, so a single deploy-window gap in canary data doesn't page |
| Time to route | p90 under 60s | Measured as `TimeToRouteSeconds` |
| Data at rest | Encrypted with a key we control | Customer-managed KMS key on table and queues |
| Data in transit | TLS only | CloudFront redirects HTTP, API Gateway is HTTPS-only |
| Recovery | Under 15 minutes | Redeploy previous commit; PITR on the table |
| Accessibility | WCAG 2.2 AA | See S4 |
| Cost | Under £15/month at this volume | Pay-per-request everywhere, no idle compute. **Caveat:** the two health-check canaries (dev + prod) alone are estimated at ~£15–20/month at their 5-minute schedule — see the cost comment in `infra/helpdesk_stack.py` — which is already at or over this target before anything else is counted. Not yet reconciled; verify against Cost Explorer after deploy |
