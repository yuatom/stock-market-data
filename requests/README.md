# Collector requests

Production on-demand collector requests live on the dedicated `collector-requests` branch, never on `main`.

`main` owns the immutable compute/data-contract version. `stock-dairy` first pins that main commit as `market_data_contract_sha`, then writes or updates `requests/collector-request.json` on `collector-requests` with the same SHA. The collector validates the pin and always checks out compute code from `main`; request-branch code is never executed as the data contract.

The request is mutable control state, not a Market Fact, Evidence object, Decision, Report authority, or completion receipt. Persisted facts are written only to private `yuatom/stock-market-data-store`.
