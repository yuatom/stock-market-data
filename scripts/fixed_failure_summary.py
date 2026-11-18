"""Bounded diagnostic vocabulary, never exception messages or causal authority."""
import argparse,json
from pathlib import Path
# Based on the fixed-vocabulary approach in draft #106, not a claim that the
# draft is merged. This runner sees exception objects, so module+class can be
# checked directly instead of inferring a name from stderr.
MODULE_CLASSES={
    'build_deterministic_metric_proof':frozenset({'MetricProofError','MetricProofUnavailable'}),
    'builtins':frozenset({'RuntimeError','ValueError','TypeError','KeyError','AssertionError','FileNotFoundError','PermissionError','ModuleNotFoundError','ImportError','OSError','TimeoutError','IndexError'}),
    'json.decoder':frozenset({'JSONDecodeError'}),
    'subprocess':frozenset({'TimeoutExpired','CalledProcessError'}),
    'market_data_collection':frozenset({'CoverageContractError'}),
    'dynamic_intraday_response':frozenset({'KnownIntradayRejection'}),
    'dynamic_daily_owner':frozenset({'DynamicPublicationUnknown'}),
    'market_data_store':frozenset({'MarketDataError','MarketDataConflict','MarketDataIdentityError','MarketDataSourceAffinityError'}),
    'market_data_collectors':frozenset({'BudgetExceeded','StoreIntegrityError'}),
    'admit_market_data_baseline':frozenset({'AdmissionError'}),
    'fixed_finalizer_offline':frozenset({'FinalizerError'}),
    'fixed_inventory_offline':frozenset({'InventoryError'}),
    'verify_store_publication':frozenset({'StorePublicationError'}),
}
CLASSES=frozenset().union(*MODULE_CLASSES.values())
STAGES=frozenset({'invocation_load','runner_execution','argument_validation','git_environment','input_validation','owner_initialization','universe_load','publisher_initialization','collection','stage_publication'})

def sanitize(value):
    return dict(status='fixed_guarded_stopped',exit_code=2,
        exception_class=value.get('exception_class') if value.get('exception_class') in CLASSES else 'unknown',
        failure_stage=value.get('failure_stage') if value.get('failure_stage') in STAGES else 'unknown')

def summary(exc,stage):
    cls=type(exc)
    name=cls.__name__ if cls.__name__ in MODULE_CLASSES.get(cls.__module__,frozenset()) else 'unknown'
    return sanitize(dict(exception_class=name,failure_stage=stage))

def read_summary(path):
    try:
        with Path(path).open('rb') as f:raw=f.read(4097)
        if len(raw)>4096:raise ValueError()
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError()
        return sanitize(value)
    except (OSError,ValueError,TypeError):return sanitize({})

def main(argv=None):
    parser=argparse.ArgumentParser();parser.add_argument('--summary',required=True)
    args=parser.parse_args(argv);print(json.dumps(read_summary(args.summary),sort_keys=True));return 0
if __name__=='__main__':raise SystemExit(main())
