# Offline-only consumer candidate consistency gate
No network, environment, runtime, repository or authorization reads/writes.
expected_consumer_commit checks only SHA syntax, never commit existence or tree membership.
Artifact bytes are opaque. Matching digest does not prove tests ran or lineage support.
All outputs are BLOCKED / production_ready=False / verified effective version None.
Missing: authoritative commit/tree validation, artifact provenance, effective runtime resolver.
No approval format, private route, workflow or frozen source changed.
Dictionary API only; no JSON interface. A future parser must reject duplicate keys.
local-candidate-association.json is a byte inventory, NOT a deployment manifest.
Candidate commit and effective version remain null: awaiting real candidate commit, never substitute baseline b7.
Synthetic tests use a*40 only, not an actual commit claim.
RED: red-contract.log, exit 1, 2 failed / 6 passed (8 methods).
Initial red.log was incomplete API errors, not behavioral RED.
GREEN: green.log, exit 0, 8 methods passed with negative subTests.
Entire standalone unittest discovery ran, not upstream repository suites.
