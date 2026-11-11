# Capture-only response evidence

This opt-in API is independent of the normal recovery caller. It makes one
provider request only when an authorized official runner calls it. No existing
legacy workflow, CLI mode, schedule, recovery failure handler, or collector calls
it. The separate hosted capture runtime remains opt-in; its public workflow
fails closed until an approved private authority channel is configured. Adding
source does not authorize a request and does not restore SOXX.

## Existing entry points remain unchanged

- `market_data_collectors._http_json` keeps its existing behavior.
- `rebase_market_data_baseline._fetch_replacement_daily` and its qualification,
  bounded revision, archive and publication callers keep their existing behavior.
- No default dp/adjust change is made to legacy requests.
- `capture_daily_response` never calls those normalization or market-write paths.

## Required external caller contract

Call `capture_daily_response(request=..., api_key=..., access=...,
before_request=..., retain_raw=..., retain_envelope=...)` only from a separately
approved capture operation. There is no automatic provider or retention fallback.

The runner must establish before entry:

1. A fresh, exact capture-only permission identifying instrument, date window,
   provider parameters, one-request limit, private evidence destination and
   expected budget cost. A consumed recovery grant is not reusable.
2. Current Compute/Store pins, source identity, current ET accounting day and
   maturity policy, global serialization and a confirmed durable one-credit debit.
3. A one-use `before_request` callback backed by the same official
   `guard_provider_request` contract used by guarded recovery. It must revalidate
   the current debit, source and deadline immediately before transmission. A plain
   local lambda does not authenticate any of these properties.
4. Private raw/envelope retention callbacks with an authorized destination and
   retained independent digests. They must write exclusively, preserve exact
   bytes, and verify persistence before acknowledging the SHA-256. This module
   checks digest acknowledgements but cannot authenticate backend durability.

The capture-specific request is a closed credential-free object. The included
`raw_response_evidence.validate_request` pins provider identity, instrument,
daily interval, explicit `adjust=splits`, explicit `dp` (-1 or 0–11), bounded
outputsize, ascending order and the exact start/end dates. Scope is currently
Nasdaq/XNAS/USD ETF, plus explicitly selected SOXX/XNMS from the public provider
profile https://twelvedata.com/markets/256481/etf/nasdaq/soxx. XNAS and XNMS are
not aliases: response MIC must equal the chosen request exactly. Request parameters are caller assertions, not attested by
provider response metadata. API keys are never added to the evidence object.

## One-request sequence

Preflight rejects unsupported endpoints before the callback or transmission.
Only `https://api.twelvedata.com/time_series` is allowed; redirects are refused
so an API-key query cannot be forwarded to another destination. The shared
transport owner reads at most 4 MiB plus one overflow-detection byte, with no
retry and the configured bounded timeout. Oversize/non-JSON/HTTP error responses
stop. A declared Content-Length must be a single valid bounded value and match
the actual byte count. Ambiguous length/transfer headers are rejected. Chunked
bodies use a capture-only strict reader: hexadecimal chunk sizes and extensions,
every data CRLF, the zero-size terminal chunk, trailer syntax and final empty CRLF
line must all validate before retention. Framing lines are limited to 8 KiB each
and framing overhead to 64 KiB in total, separately from the entity-body limit.
Framing and trailers are validated but are not part of the retained entity bytes.
Missing terminators, malformed delimiters and incomplete transfers are never
acknowledged as complete. Exactly one JSON Content-Type is required: application/json
or an application subtype ending in +json; a json substring in another media type
or a parameter is insufficient. Close-delimited responses
have no advertised length to compare, so EOF marks their transport boundary; it
cannot prove that the provider intended no further content. Rejected HTTP/header
bodies and oversized prefixes are not retained by this API.

After one successful HTTP JSON response read:

1. Retain and acknowledge the exact raw bytes before decoding/validation.
2. Create the unqualified envelope, preserving OHLCV decimal strings and raw bytes.
3. Retain and acknowledge the envelope.
4. Return `CAPTURE_RETAINED_UNQUALIFIED`, exact hashes, one attempted request and
   false market-fact/production-recovery authority.

Malformed JSON or an unsupported response still leaves already acknowledged raw
bytes available for private diagnosis. An absent, wrong or lost retention
acknowledgement stops as `capture_retention_unconfirmed`, never a retry. HTTP
failure likewise stops after one attempted request. Error text is deliberately
value-free; it must not reveal keys, response bodies or credential-bearing URLs.
The call does not refund any debit and cannot claim one was made; that belongs
to the external runner.

## What this does not prove

A valid envelope or hash acknowledgement does not authenticate the request,
provider origin, capture timestamp, destination durability, rounding method,
volume adjustment, trading-calendar completeness or real market qualification.
The API has no shared one-use ledger by itself. Repeated calls are prevented by
the external authorized runner's persistent guard, not by a process-local flag.

Raw capture is distinct from normalization, basis qualification, versioned
activation, daily consumers and report acceptance. No existing quality gate is
relaxed, and no adjusted prices/volumes are fabricated.

## Local validation scope

All new tests use invented AAA responses and synthetic credentials, injected
transport and in-memory retention. The full repository contract suite runs with
Python network blocking and token scrubbing plus file-only Git. This is not an
OS-level sandbox attestation and not evidence that a real hosted runner or
retention destination is integrated or authorized.
