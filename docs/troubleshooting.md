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

## Two you will almost certainly hit — capture them when you do

**Bedrock returning prose instead of JSON.** The model occasionally prefixes a
response with a sentence, or wraps it in code fences, and `json.loads` raises.
This is already handled as a retryable failure, but the *first* time you see it
is a real troubleshooting narrative: log line, DLQ message, hypothesis, prompt
fix. Write it down when it happens rather than reconstructing it later.

**Visibility timeout shorter than the handler timeout.** The classic
distributed-systems fault in this shape of system: the same ticket gets
processed twice concurrently because the retry became visible while the first
invocation was still running. `test_visibility_timeout_exceeds_the_handler_timeout`
guards it now. If you hit it live, the trace map showing two overlapping
invocations for one message is excellent evidence for S7.
