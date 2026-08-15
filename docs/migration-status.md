# Migration status

## Final target

Three-repository split:

- `yuatom/stock-market-data` — public Market Data Plane implementation and GitHub Actions collector runtime.
- `yuatom/stock-market-data-store` — private persisted Market Data Store only.
- `yuatom/stock-dairy` — private research, Evidence, Report, Decision, Evaluation and canonical finalization.

## Source baseline

Source repository: `yuatom/stock-dairy`.

The source inventory already contains 28 `twelve_data_basic` Daily OHLCV series. 27 symbols have a 256-session baseline from 2025-08-08 through 2026-08-14; SPCX is identity-limited to 44 sessions from 2026-06-12 through 2026-08-14. Regular-session immutable captures and Open15/Open30/Open60/Close snapshots are also present.

## Why the public repository stays public

The public repository contains only collector/store implementation, schemas, contracts and collection metadata. It must not commit third-party market-data payloads. Standard GitHub-hosted Actions can therefore execute the data-plane software without exposing persisted Twelve Data facts.

## Private store gate

Before physical migration and collector cutover:

1. create `yuatom/stock-market-data-store` as a private repository;
2. configure `TWELVE_DATA_API_KEY` in the public compute repository;
3. configure a least-privilege `MARKET_DATA_STORE_TOKEN` in the public compute repository, scoped to Contents write on `yuatom/stock-market-data-store` only.

## Identity model

A research run binds three independent identities:

- `repository_commit_sha` — `stock-dairy` research contract/runtime snapshot;
- `market_data_contract_sha` — `stock-market-data` data-plane implementation/contract snapshot;
- `market_data_read_sha` — one immutable `stock-market-data-store` data snapshot.

They must never be conflated.

## Cutover sequence

1. create and initialize the private store repository;
2. mirror `stock-dairy/sources/market-data/**` to `stock-market-data-store/data/market-data/**`;
3. verify source/target logical inventory, series ranges, record counts and canonical payload hashes;
4. install the current collector/store runtime in the public compute repository;
5. configure cross-repository write authentication and prohibit raw market data in public logs/artifacts;
6. run read-only and no-op smoke tests against the private store;
7. prove one normal settlement-mature append-only increment in the private store;
8. switch `stock-dairy` to external `market_data_contract_sha + market_data_read_sha` reads;
9. disable the old in-repo collector only after external read/write validation;
10. retain the old `stock-dairy/sources/market-data` tree as immutable rollback archive through post-cutover acceptance.

No research/Decision/Report/Evidence/Evaluation/Finalization authority moves into either data repository.
