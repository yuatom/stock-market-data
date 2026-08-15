# Migration status

## Target

Physical separation of the reusable Market Data Plane from `yuatom/stock-dairy` into `yuatom/stock-market-data`.

## Source baseline

Source repository: `yuatom/stock-dairy`.

The source inventory already contains 28 `twelve_data_basic` Daily OHLCV series. 27 symbols have a 256-session baseline from 2025-08-08 through 2026-08-14; SPCX is identity-limited to 44 sessions from 2026-06-12 through 2026-08-14. Regular-session immutable captures and Open15/Open30/Open60/Close snapshots are also present.

## Security gate

Do not mirror third-party API data while this repository is public. Before physical data migration and collector cutover:

1. repository visibility must be `private`;
2. Actions secret `TWELVE_DATA_API_KEY` must be configured in this repository.

These gates are intentionally stricter than convenience. They preserve the source Market Data Store contract and provider-data handling rules.

## Cutover sequence

1. mirror the source Market Data Store with path translation `sources/market-data/** -> data/market-data/**`;
2. verify source/target logical inventory, series ranges, record counts and canonical blob payloads;
3. install collector/store runtime and run read-only + no-op/smoke checks;
4. enable collector schedules in this repository;
5. switch `stock-dairy` to one external `market_data_read_sha` per run;
6. disable the old in-repo collector only after target write/read validation;
7. retain the source data tree as immutable migration archive until post-cutover acceptance.

No research/Decision/Report/Finalization authority moves into this repository.
