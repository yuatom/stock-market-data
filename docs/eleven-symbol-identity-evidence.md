# Eleven-symbol identity and basis evidence

Review date: 2026-11-11. Status: **not sufficient for production admission**.
This is a read-only research report, not an approved identity profile. No provider
API request, credential use, collector execution, or remote write was performed.

## Findings

The current official Twelve Data Java SDK documentation for `getTimeSeries`
describes `adjust` as defaulting to `splits`, with `all`, `splits`, `dividends`,
and `none` alternatives. It describes automatic decimal precision as the
`dp=-1` default and its example explicitly supplies `Long dp = -1L`.
The same parameter prose describes explicit decimal counts as 0 through 11;
therefore -1 is an automatic-precision sentinel, not a decimal count or a
guarantee of fixed precision or unchanged historical serialization.
Source: [official SDK MarketDataApi documentation](https://github.com/twelvedata/twelvedata-java/blob/main/docs/MarketDataApi.md),
`getTimeSeries` / `getTimeSeriesWithHttpInfo`, observed lines 1098 and 1315–1317.
This current, mutable documentation does not establish defaults at each old
ingestion time or prove the provider applied a requested mode to a symbol.

Twelve Data's [adjustment support article](https://support.twelvedata.com/en/articles/5179064-are-the-prices-adjusted)
also describes daily, weekly, and monthly prices as split-adjusted, while
intraday is unadjusted. This is general product behavior, not a per-symbol
historical-basis certificate.

## Local source evidence

`scripts/market_data_collectors.py:294–330` builds historical daily requests
with symbol, interval, outputsize, API key, and format only. It does not pin
exchange, MIC, adjust, or dp, and converts returned price strings to floats.
The same collector writes `adjustment_semantics="provider_reported"`
at line 556. That label is not equivalent evidence to `adjust=splits, dp=-1`.
Current source alone also cannot establish which historical code version
produced every retained Store row.

The approved private runtime contract supplies Store and blob identities.
Those private pins are not published in this source tree and do not establish
provider exchange/MIC mapping or request-basis proof.
`config/collection-universe.json` supplies ETF asset classification, not
provider-returned identity. Synthetic profiles in local simulation code have
no authority for real symbols. The design explicitly leaves these facts
unverified (`docs/eleven-symbol-append.md`,
identity/basis section).

## Supplement: verified public instrument profiles

The independent provider-page review supplied after the initial research pass
established the following public profile identities. Sources and observation
method are retained in `config/eleven-symbol-qualification.json`. These are
proposed capture request identities, not proof of the time_series API's returned
operating MIC or historical adjustment equivalence.

| Symbol | Public profile exchange | Public profile MIC | Source |
| --- | --- | --- | --- |
| IBIT | NASDAQ | XNMS | [Provider profile](https://twelvedata.com/markets/779898/etf/nasdaq/ibit) |
| IWM | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/141255/etf/nyse/iwm) |
| KWEB | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/785193/etf/nyse/kweb) |
| QQQ | NASDAQ | XNMS | [Provider profile](https://twelvedata.com/markets/958673/etf/nasdaq/qqq) |
| SMH | NASDAQ | XNMS | [Provider profile](https://twelvedata.com/markets/291499/etf/nasdaq/smh) |
| SPY | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/568728/etf/nyse/spy) |
| TLT | NASDAQ | XNMS | [Provider profile](https://twelvedata.com/markets/333615/etf/nasdaq/tlt) |
| VIXY | CBOE | BATS | [Provider profile](https://twelvedata.com/markets/105229/etf/cboe/vixy) |
| XLE | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/776406/etf/nyse/xle) |
| XLU | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/227896/etf/nyse/xlu) |
| XLK | NYSE | ARCX | [Provider profile](https://twelvedata.com/markets/998455/etf/nyse/xlk) |

The official [time-series documentation](https://twelvedata.com/docs/market-data/time-series)
also confirms current splits/dp=-1 defaults and one credit per symbol. Current
mutable documentation is not a historical basis or rounding certificate.

## Runtime qualification and remaining evidence

Capture permission and append qualification are deliberately separate. An exact
approved request can obtain and privately retain one response using these public
profile identities. No extra paid metadata request is needed. The raw response
must match exact symbol/exchange/MIC/currency/type/timezone, with no XNMS/XNAS or
other silent aliases. A mismatch is retained and rejected without publishing facts.

The approved conditional append policy establishes observed overlap compatibility
for this bounded operation: all approved old OHLC values must be equal as decimals
before float normalization, all approved dates must match exactly, explicit splits/dp=-1
must remain pinned and the old provider_reported semantics remain unchanged.
Historical volume differences are retained unchanged and audited by date. This
is not universal rounding/volume semantics or split-migration qualification.

The registry's stronger response-MIC and legacy-basis proof flags remain false.
They are not promoted by the current docs or by this report. The official route
uses the stricter observed-response gates rather than requiring unattainable
pre-response knowledge. If those gates fail, it records a certain rejection.

No real response, provider request, credential use, external write or execution
approval was obtained during this research. Deployment, fresh exact pins and an
explicit one-shot capture/private-retention/conditional-append approval remain
separate from the local implementation and tests.
