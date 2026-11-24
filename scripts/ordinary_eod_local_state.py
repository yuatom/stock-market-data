"""Dormant ordinary EOD state interface. No hosted adapter or issuer is registered.

The local implementation exercises the same native Git claim/debit/publish path
on an isolated caller-owned checkout; it supplies no production authorization.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

from scripts.daily_basis_store import LocalGitStore
from scripts.market_data_store import (SCHEMA_VERSION, append_daily_bars, canonical_bytes,
    git_blob_sha_bytes, quarantine_daily_volume, read_daily_exact)

ET = ZoneInfo("America/New_York")
PREFIX = "data/market-data/"
MAX_BYTES = 4 * 1024 * 1024
MAX_LOCAL_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_LOCAL_PATHS = 4096


class OrdinaryEodHeld(RuntimeError):
    """Fail closed without retrying or including private response data."""


class OrdinaryEodPreparedDrift(OrdinaryEodHeld):
    """Known departure from owned prepared bytes; no data publishes."""


def need(condition: bool) -> None:
    if not condition:
        raise OrdinaryEodHeld("HELD_ORDINARY_EOD_CLAIM_OR_PROOF")


def encode(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class OrdinaryEodLocalState:
    """Native LocalGitStore for offline integration only.

    An explicit caller passes a previously resolved fixture-only session and
    approved scope. The hosted collector has no construction/registration path.
    """

    def __init__(self, *, repo: Path, previous_session: str, cutoff: datetime,
                 scope: tuple[tuple[str, str], ...], clock, offline_fetch):
        day = date.fromisoformat(previous_session)
        need(day.isoformat() == previous_session)
        need(cutoff.tzinfo is not None and cutoff.utcoffset() is not None)
        need(scope == tuple(sorted(set(scope))) and bool(scope))
        need(all(re.fullmatch(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*", s) for s, _ in scope))
        self.store = LocalGitStore(repo)
        self.root = self.store.repo / PREFIX.rstrip("/")
        self.previous = previous_session
        self.cutoff = cutoff.astimezone(ET)
        self.scope = scope
        self.clock = clock
        self._offline_fetch = offline_fetch
        self.head = None
        self.attempted = set()
        self.day = None
        self.limits = None
        self.initial = None
        self.prepared = None
        self.effective = None
        # One ordinary lane per actual market session. An arbitrary subset or a
        # new approval must not change this key and permit a second capture.
        self.claim = PREFIX + f"collector-state/eod-admission/XNYS/{previous_session}/claim.json"
        self.terminal = self.claim.rsplit("/", 1)[0] + "/terminal.json"

    def _now(self, day: str, maturity: str) -> datetime:
        instant = self.clock()
        need(instant.tzinfo is not None and instant.utcoffset() is not None)
        now = instant.astimezone(ET)
        need(now.date().isoformat() == day == self.cutoff.date().isoformat())
        need(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", maturity) is not None)
        need(self.previous < day and now.strftime("%H:%M") >= maturity and now < self.cutoff)
        return now

    def _read_checkout(self) -> dict[str, bytes]:
        need(self.root.is_dir() and not any(p.is_symlink() for p in (self.root, *self.root.parents)))
        files = {}
        total = 0
        # No special entries, symlinks or hardlinks can be committed by this lane.
        for n, path in enumerate(self.root.rglob("*"), 1):
            need(n <= MAX_LOCAL_PATHS and not path.is_symlink())
            if path.is_dir():
                continue
            need(path.is_file() and path.stat().st_nlink == 1 and path.stat().st_size <= MAX_BYTES)
            name = PREFIX + path.relative_to(self.root).as_posix()
            payload = path.read_bytes()
            total += len(payload)
            need(len(payload) <= MAX_BYTES and total <= MAX_LOCAL_SNAPSHOT_BYTES)
            files[name] = payload
        return files

    def _matches_start_tree(self, tip: str, checkout: dict[str, bytes]) -> bool:
        raw = self.store.g.read("ls-tree", "-rz", tip, "--", PREFIX)
        objects = {}
        for line in raw.split(b"\0")[:-1]:
            record, sep, name = line.partition(b"\t")
            kind = record.split()
            if not sep or len(kind) != 3 or kind[:2] != [b"100644", b"blob"]:
                return False
            objects[name.decode("utf-8")] = kind[2].decode("ascii")
        return set(objects) == set(checkout) and all(
            hashlib.sha1(f"blob {len(b)}\0".encode() + b).hexdigest() == objects[p]
            for p, b in checkout.items()
        )

    def _publish(self, tip: str, updates: dict[str, bytes], *, replace: set[str],
                 day: str, maturity: str) -> str:
        # No rebase. Check cutoff again after potentially slow object creation.
        need(self.store.head() == tip)
        candidate = self.store.commit(tip, updates, replace=replace)
        self._now(day, maturity)
        self.store.cas("refs/heads/main", candidate, tip)
        return candidate

    def begin(self, *, root: Path, day: str, scope, identity_effective,
              daily: int, minute: int, maturity: str) -> bool:
        need(self.head is None and root.absolute() == self.root)
        self._now(day, maturity)
        need(tuple(sorted(scope)) == self.scope and 0 < daily <= 200 and 0 < minute <= 8)
        need(set(identity_effective) == set(dict(self.scope)))
        for effective in identity_effective.values():
            if effective is not None:
                need(date.fromisoformat(effective).isoformat() == effective)
        self.effective = dict(identity_effective)
        with self.store.lock():
            tip = self.store.head()
            if self.store.exists(tip, self.claim):
                return False  # consumed or unknown; never replay
            start = self._read_checkout()
            need(self._matches_start_tree(tip, start))
            claim = encode(dict(kind="ordinary_eod_session_claim_v1", state="CLAIMED_NO_REPLAY",
                                session=self.previous, execution_day=day, cutoff=self.cutoff.isoformat(),
                                scope=self.scope, identity_effective=self.effective, base=tip,
                                daily_cap=daily, minute_cap=minute))
            self.head = self._publish(tip, {self.claim: claim}, replace=set(), day=day, maturity=maturity)
            self.initial, self.day, self.limits = start, day, (daily, minute, maturity)
            self.prepared = dict(start)
            return True

    def collect_symbol(self, symbol: str, *, outputsize: int):
        need(self.head is not None and symbol not in self.attempted and symbol in dict(self.scope))
        self._assert_prepared()
        daily, minute, maturity = self.limits
        now = self._now(self.day, maturity)
        minute_key = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        budget_path = PREFIX + f"collector-state/{self.day[:7]}/{self.day}-twelve-data-budget.json"
        debit_path = self.claim.rsplit("/", 1)[0] + f"/debit-{symbol}.json"
        with self.store.lock():
            tip = self.store.head()
            need(tip == self.head and not self.store.exists(tip, debit_path))
            existing = self.store.exists(tip, budget_path)
            budget = json.loads(self.store.read(tip, budget_path)) if existing else dict(
                schema_version=1, trade_date=self.day, credits=0, minutes={}
            )
            need(set(budget) == {"schema_version","trade_date","credits","minutes"}
                 and type(budget["schema_version"]) is int and budget["schema_version"] == 1
                 and budget["trade_date"] == self.day and type(budget["credits"]) is int
                 and 0 <= budget["credits"] < daily and type(budget["minutes"]) is dict
                 and all(type(v) is int and 0 <= v <= minute for v in budget["minutes"].values())
                 and sum(budget["minutes"].values()) <= budget["credits"]
                 and budget["minutes"].get(minute_key, 0) < minute)
            for stamp in budget["minutes"]:
                need(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z", stamp))
                parsed = datetime.strptime(stamp,"%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
                need(parsed <= now and parsed.astimezone(ET).date().isoformat() == self.day)
            budget["credits"] += 1
            budget["minutes"][minute_key] = budget["minutes"].get(minute_key, 0) + 1
            budget["minutes"] = dict(sorted(budget["minutes"].items())[-16:])
            debit = encode(dict(symbol=symbol, minute=minute_key, credits=1, state="CONSUMED_NO_REPLAY"))
            self._now(self.day, maturity)
            self.head = self._publish(tip, {budget_path: encode(budget), debit_path: debit},
                                      replace={budget_path} if existing else set(),
                                      day=self.day, maturity=maturity)
        self.attempted.add(symbol)
        self._now(self.day, maturity)
        # No provider key, endpoint or live callable is resolved by the collector.
        self._assert_prepared()
        fetched = self._offline_fetch(symbol, outputsize=outputsize)
        self._assert_prepared()
        return fetched

    def _assert_prepared(self) -> None:
        try:
            snapshot = self._read_checkout()
        except OrdinaryEodHeld:
            raise OrdinaryEodPreparedDrift("HELD_PREPARED_DRIFT") from None
        if snapshot != self.prepared:
            raise OrdinaryEodPreparedDrift("HELD_PREPARED_DRIFT")

    def _install_prepared(self, changes: dict[str, bytes]) -> None:
        self._assert_prepared()
        for path,payload in changes.items():
            need(path.startswith(PREFIX) and len(payload) <= MAX_BYTES)
            self.store.path(path)
            dest = self.root / path.removeprefix(PREFIX)
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(payload)
        self.prepared.update(changes)

    def write_evidence(self, relative: str, raw: bytes) -> None:
        path = PREFIX + relative
        state_dir = f"collector-state/{self.day[:7]}/"
        expected = state_dir + "price-only-" + hashlib.sha256(raw).hexdigest() + ".json"
        need(relative == expected and path not in self.prepared)
        self._install_prepared({path:raw})

    def write_diagnostic(self, relative: str, raw: bytes) -> None:
        path = PREFIX + relative
        expected = re.escape(f"collector-state/{self.day[:7]}/{self.day}-previous_session_eod-")
        need(re.fullmatch(expected + r"[0-9a-f]{16}-conflicts\.json",relative)
             and path not in self.prepared)
        self._install_prepared({path:raw})

    def _common_write(self, root: Path, op: str, *, provider: str, symbol: str, **kwargs):
        """Derive series bytes from the owned state using native common writes.

        The separate preparation tree contains exact prior owned bytes. A callback
        or altered worktree cannot become the input to the owned append.
        """
        need(root == self.root and provider == "twelve_data_basic" and symbol in self.attempted)
        self._assert_prepared()
        prefix = PREFIX + f"series/daily/{provider}/{symbol}/"
        own_series = {path:raw for path,raw in self.prepared.items() if path.startswith(prefix)}
        own_inputs = dict(own_series)
        if op == "quarantine":
            operation = quarantine_daily_volume
            evidence = PREFIX + kwargs["evidence_path"]
            own_inputs[evidence] = self.prepared[evidence]
        elif op == "append":
            operation = append_daily_bars
            need(kwargs.get("asset_class") == dict(self.scope)[symbol]
                 and kwargs.get("identity_effective_from") == self.effective[symbol])
        else:
            need(False)
        with TemporaryDirectory(prefix="ordinary-eod-owned-series-") as tmp:
            temp_root = Path(tmp)
            for path,raw in own_inputs.items():
                dest = temp_root / path.removeprefix(PREFIX)
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(raw)
            result = operation(temp_root,provider=provider,symbol=symbol,**kwargs)
            series_dir = temp_root / prefix.removeprefix(PREFIX)
            generated = {}
            if series_dir.exists():
                for dest in series_dir.iterdir():
                    need(dest.is_file() and not dest.is_symlink()
                         and re.fullmatch(r"(?:_index|[0-9]{6})\.json",dest.name))
                    generated[prefix+dest.name] = dest.read_bytes()
            need(set(own_series) <= set(generated))
            changed = {p:b for p,b in generated.items() if own_series.get(p) != b}
        self._install_prepared(changed)
        return result

    def append_series(self, root: Path, **kwargs):
        return self._common_write(root,"append",**kwargs)

    def quarantine_series(self, root: Path, **kwargs):
        return self._common_write(root,"quarantine",**kwargs)

    def reject_prepared(self) -> str:
        # Only a known drift gets this terminal; uncertain ref/ACK never appears
        # as a known preparation failure.
        with self.store.lock():
            tip = self.store.head()
            need(self.head == tip and not self.store.exists(tip,self.terminal))
            receipt = encode(dict(kind="ordinary_eod_local_terminal_v1",
                    state="FAILED_PREPARED_DRIFT_NO_REPLAY", parent=tip,
                    target_session=self.previous, missing=[],targets={},outputs={}))
            self.head = self._publish(tip,{self.terminal:receipt},replace=set(),
                                      day=self.day,maturity=self.limits[2])
        return self.head

    def _target_proofs(self, snapshot: dict[str, bytes]) -> tuple[dict, list[str]]:
        """Check the same frozen bytes that may be written in the terminal CAS."""
        proofs, missing = {}, []
        with TemporaryDirectory(prefix="ordinary-eod-proof-") as tmp:
            root = Path(tmp)
            for symbol, asset_class in self.scope:
                relative = f"series/daily/twelve_data_basic/{symbol}/"
                prefix = PREFIX + relative
                index_path = prefix + "_index.json"
                if index_path not in snapshot:
                    need(not any(path.startswith(prefix) for path in snapshot))
                    missing.append(symbol)
                    continue
                index_raw = snapshot[index_path]
                index = json.loads(index_raw)
                need(type(index) is dict and canonical_bytes(index) + b"\n" == index_raw)
                need(type(index.get("schema_version")) is int and index["schema_version"] == SCHEMA_VERSION)
                expected = {"series_id": f"twelve_data_basic:{symbol}:regular:1d",
                    "provider": "twelve_data_basic", "symbol": symbol, "asset_class": asset_class,
                    "session": "regular", "interval": "1d", "series_semantics": "daily_regular_ohlcv",
                    "adjustment_semantics": "provider_reported", "identity_effective_from": self.effective[symbol]}
                need(all(index.get(key) == value for key, value in expected.items()))
                shards = index.get("shards")
                need(isinstance(shards, list))
                paths = [m["path"] for m in shards]
                need(len(paths) == len(set(paths)) and all(re.fullmatch(r"[0-9]{6}\.json", x) for x in paths))
                piece_paths = [index_path, *(prefix + name for name in paths)]
                dest = root / relative
                dest.mkdir(parents=True)
                for path in piece_paths:
                    (dest / path.rsplit("/", 1)[1]).write_bytes(snapshot[path])
                for meta,path in zip(shards,piece_paths[1:]):
                    need(git_blob_sha_bytes(snapshot[path]) == meta["blob_sha"])
                row = read_daily_exact(root,provider="twelve_data_basic",symbol=symbol,trade_date=self.previous)
                if row is None:
                    missing.append(symbol)
                    continue
                effective = self.effective[symbol]
                need(effective is None or effective <= self.previous)
                prices = [row.get(field) for field in ("open","high","low","close")]
                need(all(type(v) in (int,float) and math.isfinite(v) and v > 0 for v in prices))
                opened, high, low, closed = prices
                need(low <= min(opened,closed) <= max(opened,closed) <= high)
                found = [path for path in piece_paths[1:] if row in json.loads(snapshot[path])["records"]]
                need(len(found) == 1)
                proof_paths = [index_path,found[0]]
                proofs[symbol] = dict(source="ALREADY_DURABLE" if all(
                    self.initial.get(path) == snapshot[path] for path in proof_paths) else "PREPARED_APPEND",
                    target_session=self.previous, index_path=index_path, index_sha256=hashlib.sha256(index_raw).hexdigest(),
                    shard_path=found[0], shard_sha256=hashlib.sha256(snapshot[found[0]]).hexdigest(),
                    row_sha256=hashlib.sha256(encode(row)).hexdigest())
        return proofs,missing

    def finish(self) -> tuple[str,bool]:
        need(self.attempted == set(dict(self.scope)))
        self._now(self.day, self.limits[2])
        self._assert_prepared()
        # The owned frozen outputs, not the live mutable checkout, are the
        # sole input to validation and the exact terminal transaction.
        after = dict(self.prepared)
        need(set(self.initial) <= set(after))
        updates = {p: b for p,b in after.items() if self.initial.get(p) != b}
        evidence = re.compile(re.escape(PREFIX + f"collector-state/{self.day[:7]}/") +
             r"(?:price-only-[a-f0-9]{64}|" + re.escape(self.day) +
             r"-previous_session_eod-[a-f0-9]{16}-conflicts)\.json")
        for p in updates:
            relative = p.removeprefix(PREFIX)
            match = re.fullmatch(r"series/daily/twelve_data_basic/([^/]+)/(?:_index|\d{6})\.json", relative)
            need((match and match[1] in dict(self.scope)) or (evidence.fullmatch(p) and p not in self.initial))
        proofs, missing = self._target_proofs(after)
        # Known incomplete owns its spent budget but must not publish older or
        # other-symbol scratch outputs as a partial successful EOD.
        complete = not missing
        if not complete:
            updates = {}
        terminal = encode(dict(kind="ordinary_eod_local_terminal_v1",
                state="COMPLETE" if complete else "INCOMPLETE_NO_REPLAY", parent=self.head,
                target_session=self.previous, missing=missing, targets=proofs if complete else {},
                outputs={p: hashlib.sha256(b).hexdigest() for p,b in updates.items()}))
        with self.store.lock():
            tip = self.store.head()
            need(tip == self.head)
            self._now(self.day, self.limits[2])
            self.head = self._publish(tip, {**updates, self.terminal: terminal},
                                      replace=set(updates) & set(self.initial),
                                      day=self.day, maturity=self.limits[2])
        return self.head,complete
