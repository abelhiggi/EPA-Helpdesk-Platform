# Troubleshooting log

## How I debug this system

Every request gets a correlation ID in `ingest.py`, stored on the ticket as
`correlationId` and carried on the SQS message body, so I can follow one
ticket into `process.py`. Logs are structured JSON, so I use Logs Insights in
the console, filtering on correlationId across the ingest and process log
groups instead of reading raw lines. X-Ray
tracing is on for the API and all three functions, and I use the trace map
for timing questions, like whether something ran or how long it took. Most
of the time I notice a problem on the dashboard or from an alarm first. From
there I pull the correlation ID off the ticket in DynamoDB, query the logs
for it, and only open X-Ray if the logs don't explain what happened.

## 2026-08-26: ticket stuck at NEW

**Symptom.** A test ticket (`ed7af962-30fa-4f58-8ddd-0b23d6f130ea`) stayed at
status `NEW` on `GET /tickets/{id}` well past normal routing time. I polled
it six times over 30 seconds and it never changed. `TicketsSubmitted` had
fired but `CategorisationConfidence` and `TimeToRouteSeconds` had not, and
both only fire once `process.py` finishes classifying. The message had
already reached the DLQ and been parked there after `redrive.py`'s own
retries ran out.

**Hypothesis.** I first assumed ingest had never enqueued the message, but
the ticket already existed in DynamoDB with the right description and
correlation ID, and the API had returned 201, so that was wrong. I checked
whether process had even been invoked, but its log group showed a normal
START, END, REPORT cycle in about 1.2 seconds, so it ran and ran fast. I
then suspected the SQS message body itself was malformed. I invoked the
deployed process function directly with a hand built event containing a
body I knew was valid, and it failed the same way, which ruled that out and
pointed at something inside `_process_one`.

**How I checked.** `aws logs tail` on the process log group showed one line,
a `JSONDecodeError`, tied to the invocation's request ID, but the handler
doesn't log the raw event, so it didn't say which parse failed. Invoking the
function directly with a hand built SQS event reproduced the same error on
demand. I called `bedrock-runtime converse` directly with the same prompt,
model ID and temperature that `process.py` uses, for the same ticket
description, and got back valid categorisation JSON wrapped in a markdown
code fence the prompt explicitly says not to use. `json.loads` chokes on the
leading backtick, and that was the failure. `aws cloudwatch list-metrics`
confirmed `CategorisationConfidence` and `TimeToRouteSeconds` had never been
emitted for this ticket, which matched.

**Fix.** In `src/process.py`, `_extract_json()` now strips a wrapping code
fence before parsing, and the `json.loads` call is wrapped in
`try`/`except JSONDecodeError`, falling back to the same keyword rule an
out of taxonomy category already used. `ClassificationFallbacks` now fires
on both paths into the fallback, so this shows on the dashboard instead of
failing silently. Tests:
`test_fenced_json_response_with_language_tag_parses_correctly`,
`test_bare_fenced_json_response_parses_correctly`,
`test_unparseable_model_output_falls_back_instead_of_raising`,
`test_fenced_response_with_out_of_taxonomy_category_still_falls_back`,
`test_fallback_emits_a_classification_fallbacks_metric`.

**What I learned / follow-up.** The taxonomy check one line below the parse
already existed to handle a model response that can't be trusted. An
unparseable response is the same class of problem, and I should have put it
on that path from the first implementation, instead of leaving it to
whatever the SQS handler's generic exception catch did with it. Any parse of
an external system's output needs the same treatment.

## 2026-09-03: dashboard widgets showing no data

**Symptom.** Three dashboard widgets, categorisation confidence, time to
route, and classification fallbacks, showed no data even though tickets were
flowing normally. The tickets by category widget on the same dashboard
worked and showed real numbers.

**Hypothesis.** I assumed the widgets were querying a metric that didn't
exist under the name and dimensions they expected, most likely a dimension
mismatch, since a metric's dimensions are part of its identity in
CloudWatch. A query with different dimensions matches a different metric,
one that was never emitted.

**How I checked.** I compared `emit_metric` in `src/common.py`, which always
adds `Service` and `Environment` and adds `Category` when it is passed, with
the bare `cw.Metric` calls for these three widgets in
`infra/helpdesk_stack.py`. Categorisation confidence and time to route are
emitted with a `Category` dimension in `process.py`, but the widgets queried
them with no dimensions at all. Classification fallbacks is emitted without
a `Category`, but the widget still queried it with no `Service` or
`Environment` dimension either, so it matched nothing either.

**Fix.** For categorisation confidence and time to route I built one
`cw.Metric` per category, network and software, each with `Service`,
`Environment` and `Category` set to match what `process.py` emits. For
classification fallbacks I added `Service` and `Environment` to the single
existing `cw.Metric`, with no `Category`, since that metric is never emitted
with one. Commit `cd32809`, "fix: dashboard widgets missing
Service/Environment dimensions on custom metrics", 2026-09-03.

**What I learned / follow-up.** The CDK tests only check that the dashboard
resource exists. They don't check that its widgets use the same dimensions
`emit_metric` actually produces. That's still open.
