# Troubleshooting log

The pass criteria ask for a worked example of identifying and remediating an
issue that compromised code quality, and for troubleshooting steps taken to
locate an issue across the end-to-end service. **Fill these in from real
incidents as you build — invented ones do not survive questioning.**

Structure each entry like this:

## [Date] — [One-line symptom]

**Symptom.** What was observed, and where. Which dashboard widget, log line or
test failure told you.

**Hypotheses.** List them, including the ones that turned out to be wrong.
Ruling something out is evidence of a systematic approach; arriving instantly
at the right answer looks like hindsight.

**How each was tested.** The specific query, command or experiment. For
distributed faults, name the tool: `aws logs start-query` with the correlation
ID, the X-Ray trace map, `cdk diff`.

**Root cause.** The actual mechanism, not the surface.

**Fix.** The change, plus the test that now fails if it regresses.

**Prevention.** What stops the whole class of issue recurring.

---

## 2026-08-26 — Ticket stuck at NEW, never reaches ROUTED

**Symptom.** A test ticket submitted end-to-end during dev deployment
verification (`ed7af962-30fa-4f58-8ddd-0b23d6f130ea`) stayed at `status: NEW`
on `GET /tickets/{id}` well past the time routing normally takes — polled six
times over 30 seconds, unchanged every time. `EPA/Helpdesk` showed
`TicketsSubmitted` (emitted by `ingest.py` at submission) but neither
`CategorisationConfidence` nor `TimeToRouteSeconds`, both of which only fire
after a successful classification in `process.py` — meaning `_process_one`
was starting but never finishing. The message was later confirmed to have
reached the DLQ and been parked there after `redrive.py`'s own retries were
exhausted; `DlqDepthAlarm` (threshold ≥1 over one minute) is wired to fire
exactly on that transition.

**Hypotheses.**

1. *Ingest never enqueued the message.* Ruled out immediately: the ticket
   existed in DynamoDB with `status: NEW`, the correct `description` and
   `correlationId`, and the API had returned `201` — ingest's own job was
   already done correctly.
2. *Process was never invoked at all* (an event-source-mapping or permissions
   problem). Ruled out: `aws logs tail` on the Process function's log group
   showed a `START`/`END`/`REPORT` cycle at the expected time — it ran, and
   ran fast (~1.2s), so it wasn't hanging either.
3. *The SQS message body itself was malformed* — the outer
   `json.loads(record["body"])` in the `handler` failing, not the classify
   step. Tested directly: invoked the deployed Process Lambda with a
   hand-built SQS event containing an unquestionably valid body
   (`{"ticketId": ..., "correlationId": ..., "createdAt": ...}`), bypassing
   SQS timing entirely. It failed identically. This ruled out the message
   body and pointed at something inside `_process_one` itself.
4. *(confirmed)* The failure is inside `_classify`, on the Bedrock response,
   not the SQS message. Tested by calling `bedrock-runtime converse` directly
   with the exact prompt, model ID and inference profile `process.py` uses,
   for the same ticket description. The raw response settled it.

**How each was tested.**

- `aws logs tail <ProcessLogGroup> --since 10m` — showed the single log line
  `{"level": "ERROR", "message": "processing failed", "reason":
  "JSONDecodeError"}`, correlated to the invocation's `RequestId`. This
  narrowed it to *some* `json.loads` call failing, but the handler's
  `error()` call deliberately doesn't log the raw event content, so it didn't
  say which one.
- `aws lambda invoke --function-name <Process> --payload <hand-built SQS
  event>` — reproduced the exact same `JSONDecodeError` on demand, ruling out
  hypothesis 3 and anything timing- or queue-specific.
- `aws bedrock-runtime converse` with the identical prompt template, model ID
  (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`) and `temperature: 0.0` used
  in `process.py`, for the description `"VPN keeps dropping every 10 minutes
  on the third floor"` — returned:

  ```
  RAW TEXT REPR: '```json\n{"category": "network", "priority": "high", "confidence": 0.95}\n```'
  JSON PARSE FAILED: JSONDecodeError Expecting value: line 1 column 1 (char 0)
  ```

  Confirming hypothesis 4 directly: valid categorisation JSON, wrapped in a
  markdown code fence the prompt explicitly says not to use, and
  `json.loads` chokes on the leading backtick.
- `aws cloudwatch list-metrics --namespace EPA/Helpdesk` — confirmed
  `CategorisationConfidence` and `TimeToRouteSeconds` had never been emitted,
  corroborating that `_classify` was the failure point (both metrics are
  emitted after it succeeds).
- `aws sqs get-queue-attributes` on the ticket queue and DLQ — watched
  `ApproximateNumberOfMessagesNotVisible` hold at 1 while the message cycled
  through its SQS-driven retries, confirming it wasn't lost, just endlessly
  retrying the same failure.

**Root cause.** An unguarded parse of untrusted model output. `_classify` in
`src/process.py` called `json.loads(text)` directly on the raw Bedrock
response, with no defensive extraction and no `try`/`except` around it. Haiku
4.5 wrapped its answer in a ` ```json ` fence despite the prompt instructing
otherwise; the resulting `JSONDecodeError` was never caught locally, so it
propagated up through `_process_one` to the SQS batch handler's generic
`except Exception`, which correctly treats *any* uncaught exception as a
retryable per-message failure. `temperature=0.0` made this deterministic —
every retry sent the identical prompt and got back the identical fenced
response, so the "retry" was never actually a chance at a different outcome.
It was not an intermittent fault; it was reproducible on demand, every time,
which is exactly why it could be pinned down directly rather than chased.
After `maxReceiveCount` (3) retries the message moved to the DLQ, the
`DlqDepthAlarm` fired, `redrive.py` made its own two attempts (same
deterministic failure both times), and the message was parked permanently.

**Fix.** Two layers, in `src/process.py`:

1. `_extract_json()` structurally strips a wrapping code fence before
   parsing — anchored to the whole string with `re.DOTALL`, greedy on the
   captured body, so it only ever strips a fence that wraps the *entire*
   response. A stray backtick inside a genuine value can't be mistaken for
   the fence boundary, because greedy matching backtracks from the end of
   the string to find the real closing fence. This is defence in depth, not
   a substitute for the prompt's existing instruction to return bare JSON —
   that instruction is unchanged.
2. The `json.loads` call is now wrapped in `try`/`except
   json.JSONDecodeError`, routing to the same `_fallback()` keyword-rule path
   an out-of-taxonomy category already used one check later in the same
   function. A `ClassificationFallbacks` metric fires on both paths into
   `_fallback`, so this is now visible on the dashboard rather than silent.

   Tests that now fail if this regresses:
   `tests/test_process.py::TestCategorisation::test_fenced_json_response_with_language_tag_parses_correctly`
   (uses the literal raw response captured above),
   `test_bare_fenced_json_response_parses_correctly`,
   `test_unparseable_model_output_falls_back_instead_of_raising`,
   `test_fenced_response_with_out_of_taxonomy_category_still_falls_back`, and
   `test_fallback_emits_a_classification_fallbacks_metric`.

**Prevention.** The taxonomy check one line below the parse already existed
specifically to handle "the model produced output that cannot be trusted" —
falling back to the keyword rule rather than writing nonsense to the table.
An unparseable response is the same class of problem, arguably a more
fundamental version of it (you cannot even check the taxonomy of something
you cannot parse), and should have been on that same fallback path from the
first implementation rather than left to whatever the top-level SQS handler
does with an uncaught exception by default. The general lesson: any parse of
an external system's output — an LLM response, a third-party API body,
anything not under this codebase's control — needs to be wrapped and routed
to the same degrade-gracefully path as its own semantic validation, not left
to a generic exception handler that cannot tell "transient and worth
retrying" apart from "deterministic and will never succeed."

---

## One you will almost certainly hit — capture it when you do

**Visibility timeout shorter than the handler timeout.** The classic
distributed-systems fault in this shape of system: the same ticket gets
processed twice concurrently because the retry became visible while the first
invocation was still running. `test_visibility_timeout_exceeds_the_handler_timeout`
guards it now. If you hit it live, the trace map showing two overlapping
invocations for one message is excellent evidence for S7.
