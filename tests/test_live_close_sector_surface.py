from pathlib import Path
import json
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collection_universe as collection
import collect_market_data as entry


def test_live_close_adds_sector_group_without_expanding_intraday_group():
    universe = collection.load_collection_universe(ROOT / "config/collection-universe.json")
    intraday = {symbol for symbol, _asset in collection.intraday_universe(universe)}
    sectors = set(collection.sector_symbols(universe))
    close_symbols = {symbol for symbol, _asset in entry._live_close_universe(universe)}

    assert sectors
    assert sectors.isdisjoint(intraday)
    assert close_symbols == intraday | sectors


def test_close_wrapper_owns_only_close_modes():
    text = (ROOT / "scripts/collect_market_data.py").read_text(encoding="utf-8")
    assert 'mode not in {"close", "close_retry", "close_final"}' in text
    assert 'eligible_universe=eligible' in text
    assert '"live_close_sector_surface"' in text
