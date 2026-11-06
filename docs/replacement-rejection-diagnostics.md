# Value-free replacement rejection diagnostics

Recovery now preserves the failed candidate's trusted, normalized symbol as
`failed_symbol`, and a `replacement_rejection` object in the retained terminal
report and CLI failure summary. The symbol is bound at the actual qualification
failure, using the already validated target selection, not inferred from budget
counts, request order, provider fields, or arbitrary exception attributes.

`rejection_rule` is emitted at the rejecting check: insufficient replacement
records, duplicate replacement dates, missing existing dates, revision outside
the authorized tail, revised overlap limit, or replacement hash mismatch.
Explicit replacement numeric checks and normalizer data-validation exceptions
are classified as invalid replacement bar. Unknown failures (including generic
canonicalization/helper exceptions) remain `unclassified`. A failure inside the
qualification boundary retains its original failure code while gaining the
trusted symbol and unclassified context. Provider, budget, source-guard and
publication failures outside that boundary do not gain qualification context. Empty-input validator
rules are also typed; qualification does not claim those rules if an earlier
check failed. No classification parses exception messages or re-runs validation.

Only fixed rule codes, integer (not boolean) counts from 0 through 5000, strict
ASCII ISO calendar dates, and at most eight missing dates are projected. Missing
or malformed evidence is omitted; invalid rules become `unclassified`. No OHLCV
values, arbitrary field names, raw errors, canonical hashes, URLs or credentials
are copied into replacement failure evidence. Existing private successful
revision/archive records are unchanged. The old comparison helper calls remain
in their original order to preserve even their fail-closed exception paths; their
raw differences are discarded rather than exported in failure reports.

No validation conditions, limits, ordering, selection, budget or HTTP logic,
workflow, configuration, schema, publication/retry behavior are changed. All
candidates must still qualify before any market-fact staging or publication.
Unknown publication outcomes still prevent another diagnostic write or retry.
If terminal report publication fails, the CLI retains the original replacement
reason separately from the publication failure.

Tests use synthetic rows and mocked provider responses with temporary local Git
remotes. Run Python 3.12 with installed project dependencies:

    PYTHON=/path/to/python3.12 sh scripts/run_contract_tests.sh

For an isolated review, deny all OS network access, isolate HOME and Git config,
allow only local file Git transport, and restrict writes to the candidate and a
disposable test directory. The incident-specific delivery includes that runner,
sandbox profile, original red logs, final test logs and source checksums.

This change cannot reconstruct the missing symbol or rule for run 42138132860.
The historical terminal report only retained `replacement_rejected`; seven
confirmed predebits do not establish which symbol failed. All thirteen selected
histories remained unapplied. Earlier volume conflicts are not evidence of the
actual rule that rejected that recovery. No provider data was reacquired.
