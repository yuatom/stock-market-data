"""Review-only bounded readback for #110 original run evidence; never writes.

No CLI, branching, network client, reauthorization, provider or retry. Caller
supplies a separately authorized, exact private origin/object view and trusted
receipt scope. Git only reads objects already present and exact remote refs.
This is not the completion gate. A count is historical durable evidence, not
the current latest market value, proof of no other effects, or retry authority.
"""
from datetime import date
import json
import re

import backfill_market_data_tail as tail
import maintain_fixed_symbols as native
import private_fixed_tail_readback as complete
from scripts.private_fixed_tail_20261120 import SCOPE, CUTOFF, BASELINE
from scripts.market_data_store import git_blob_sha_bytes

DIAGNOSTIC = "PRIVATE_FIXED_TAIL_OUTCOME_UNKNOWN_NO_RETRY"
MAX_COMMITS = 64
ROUTE = "fixed_daily_tail_20261120"
LANE = "FIXED-TAIL-1120"


def _need(value):
    if not value:
        raise ValueError("PRIVATE_FIXED_TAIL_DIAGNOSTIC_UNVERIFIED")


def _hex(value, n=40):
    return type(value) is str and re.fullmatch(r"[a-f0-9]{%d}" % n, value) is not None


def _git(repo,*args):
    return native.a.git(repo,*args)


def _tag(repo,sha):
    ref=f"refs/tags/market-data-read/{sha}"
    _need(_git(repo,"ls-remote","--refs","origin",ref)==f"{sha}\t{ref}")


def _object(repo,sha,path):
    return complete._object(repo,sha,path)


def _matches(value, expected):
    complete._matches(value,expected)


def _members(repo, start, end, paths, child_paths):
    """Read a complete bounded first-parent span, exact known paths only."""
    _need(start!=end)
    raw=_git(repo,"rev-list","--first-parent","--max-count="+str(MAX_COMMITS+1),
             "--reverse",f"{start}..{end}").splitlines()
    _need(0<len(raw)<=MAX_COMMITS and all(_hex(s) for s in raw) and raw[-1]==end)
    parent=start
    members={}
    history={}
    order={}
    for i,sha in enumerate(raw):
        _need(_git(repo,"rev-list","--parents","-n1",sha).split()==[sha,parent])
        order[sha]=i
        lines=_git(repo,"diff-tree","--no-commit-id","--no-renames","--name-status","-r",sha,"--",*paths)
        for line in lines.splitlines():
            row=line.split("\t")
            _need(len(row)==2 and row[1] in paths)
            action,path=row
            if path in child_paths:
                history.setdefault(path,[]).append((action,sha))
            else:
                _need(action=="A" and path not in members)
                members[path]=sha
        parent=sha
    return members,history,order


def _parent(repo,sha):
    values=_git(repo,"rev-list","--parents","-n1",sha).split()
    _need(len(values)==2 and values[0]==sha and _hex(values[1]))
    return values[1]


def _changed(repo,sha):
    return set(_git(repo,"diff-tree","--no-commit-id","--name-only","-r",sha).splitlines())


def _metadata_version(repo,sha,parent,report_path,budget,day,request_id):
    # Same unspent accounting and report-only manifest used by RecoveryPublisher.report.
    _need(_parent(repo,sha)==parent and _changed(repo,sha)=={report_path,budget})
    previous=native._immutable_budget(repo,parent,budget)
    current=native._immutable_budget(repo,sha,budget)
    _need(all(current[k]==previous[k] for k in ("credits","minutes")))
    outputs=[{"path":report_path,"blob_sha":native._immutable_blob(repo,sha,report_path)}]
    native.r.validate_manifest(current,outputs,request_id,day,mode=tail.MODE)
    _tag(repo,sha)
    return current


def _child_report(repo,sha,path,context,status,count):
    record=_object(repo,sha,path)
    _matches(record,dict(context,status=status,requests_attempted=count,budget_debits=count))
    _need(type(record["schema_version"]) is int and type(record["requests_attempted"]) is int
          and type(record["budget_debits"]) is int and record.get("market_fact_authority") is False
          and record.get("production_recovery_accepted") is False)
    return record


def _child_debit(repo,initial,debit,path,budget,cursor,context):
    day,request=context["trade_date"],context["request_id"]
    first_state=_metadata_version(repo,initial,cursor,path,budget,day,request)
    record=_child_report(repo,initial,path,context,"IN_PROGRESS",0)
    _need(type(record.get("started_at")) is str and "retained_normalized_response" not in record)
    _need(debit is not None and _parent(repo,debit)==initial and _changed(repo,debit)=={budget})
    debit_state=_object(repo,debit,budget)
    outputs=[{"path":path,"blob_sha":native._immutable_blob(repo,initial,path)}]
    native.r.validate_manifest(debit_state,outputs,request,day,mode=tail.MODE)
    native._one_debit(first_state,debit_state)
    _need({k:v for k,v in debit_state.items() if k not in ("credits","minutes")}
          =={k:v for k,v in first_state.items() if k not in ("credits","minutes")})
    _tag(repo,debit)
    return record


def _child_terminal(repo,retained,terminal,debit,initial_report,path,budget,context,symbol):
    _metadata_version(repo,retained,debit,path,budget,context["trade_date"],context["request_id"])
    _need(_parent(repo,terminal)==retained)
    a=initial_report
    b=_child_report(repo,retained,path,context,"RESPONSE_RETAINED_UNQUALIFIED",1)
    c=_child_report(repo,terminal,path,context,"CANDIDATE_VALIDATED",1)
    _need(type(a.get("started_at")) is str and a["started_at"]==b.get("started_at")==c.get("started_at"))
    response=b.get("retained_normalized_response")
    _need(type(response) is dict and set(response)=={"symbol","records","authority"}
          and response.get("symbol")==symbol and response.get("authority")=="normalized_not_raw_http"
          and type(response.get("records")) is list and 0<len(response["records"])<=5000
          and "retained_normalized_response" not in a and "retained_normalized_response" not in c)
    _need(c.get("budget_parent")==retained and c.get("publication_requires_terminal_receipt") is True)
    return terminal


def diagnose(h,repo,*,observed_main,approval_store_sha,operational_store_sha,commitment,
             plan_sha,compute_sha,execution_date,historical_cutoff,request_id,fixed_selector):
    """Return a value-free, verified prefix only; no full result or action.

    Internal project debits are a lower bound, independent of published
    prices and checkpoints. They attest to neither provider calls nor charges.
    """
    facts={"status":DIAGNOSTIC,"verified_price_rows":0,"verified_checkpoints":0,
           "verified_volume_unresolved":0,"prefix_evidence_available":False,
           "later_main_observed":False,"automatic_retry_allowed":False,
           "verified_project_credit_debits_lower_bound":0,
           "provider_requests":"UNKNOWN","provider_charges":"UNKNOWN"}
    durable=[]; checked=0; debits=0; anchor=None
    try:
        for sha in (observed_main,approval_store_sha,operational_store_sha,plan_sha,compute_sha):
            _need(_hex(sha))
        _need(_hex(commitment,64) and historical_cutoff==CUTOFF
              and type(execution_date) is str and date.fromisoformat(execution_date).isoformat()==execution_date
              and execution_date>CUTOFF and type(request_id) is str and re.fullmatch(r"[1-9][0-9]{0,19}-1",request_id))
        targets=fixed_selector["targets"]
        _need(type(fixed_selector) is dict and set(fixed_selector)=={"schema_version","targets"}
              and type(fixed_selector["schema_version"]) is int and fixed_selector["schema_version"]==1
              and type(targets) is list and tuple(t["symbol"] for t in targets)==SCOPE)
        _need(all(type(t) is dict and set(t)=={"symbol","index_blob_sha"} and _hex(t["index_blob_sha"]) for t in targets))
        native._remote_exact(repo,observed_main)

        claim=f"capture-evidence/{commitment}/{LANE}/claim.json"
        v=_object(repo,operational_store_sha,claim)
        claim_expected=dict(schema_version=1,kind="fixed_daily_tail_batch_claim",route=ROUTE,
          approval_commitment=commitment,reviewed_store_sha=approval_store_sha,compute_sha=compute_sha,
          execution_date=execution_date,historical_cutoff=CUTOFF,request_id=request_id)
        _matches(v,claim_expected)
        _need(type(v["schema_version"]) is int and set(v)==set(claim_expected)|{"physical_attempt"}
              and _hex(v["physical_attempt"],64))
        raw=(json.dumps(v,sort_keys=True,separators=(",",":"))+"\n").encode("ascii")
        _need(native._immutable_blob(repo,operational_store_sha,claim)==git_blob_sha_bytes(raw))
        _need(_git(repo,"rev-list","--parents","-n1",operational_store_sha).split()==[operational_store_sha,approval_store_sha])
        _need(_git(repo,"diff-tree","--no-commit-id","--no-renames","--name-status","-r",operational_store_sha)==f"A\t{claim}")
        _need(_git(repo,"rev-parse",approval_store_sha+":data/market-data")
              ==_git(repo,"rev-parse",operational_store_sha+":data/market-data"))
        _tag(repo,operational_store_sha)
        for target in targets:
            sym=target["symbol"];index_path=f"{native.r.PREFIX}series/daily/twelve_data_basic/{sym}/_index.json"
            _need(native._immutable_blob(repo,operational_store_sha,index_path)==target["index_blob_sha"])
            # The actual initial row boundary is a requirement, not implied by
            # the commit or merely the profile's constant cutoff.
            _,old_rows,_=native._immutable_series(repo,operational_store_sha,sym,h.store)
            _need(old_rows[-1]["trade_date"]==BASELINE)

        prefix=f"{native.r.PREFIX}collector-state/{execution_date[:7]}/{execution_date}"
        budget=f"{prefix}-twelve-data-budget.json"
        plan=f"{prefix}-daily-symbol-maintenance-{request_id}.json"
        part={s:f"{prefix}-daily-history-tail-backfill-{request_id}-{s}.json" for s in SCOPE}
        checkpoint={s:f"{prefix}-daily-symbol-maintenance-{request_id}-{s}.json" for s in SCOPE}
        members,history,order=_members(repo,operational_store_sha,observed_main,
                               [claim,plan,*part.values(),*checkpoint.values()],set(part.values()))
        _need(members.get(plan)==plan_sha)
        native._metadata_proof(repo,plan_sha,operational_store_sha,plan,execution_date,request_id)
        common=dict(compute_sha=compute_sha,store_start_sha=operational_store_sha,execution_date=execution_date,
                    historical_cutoff=CUTOFF,request_id=request_id,automatic_retry_allowed=False)
        _matches(_object(repo,plan_sha,plan),dict(common,kind="append_only_batch_plan",mode=native.MODE,
              status="PLANNED",trade_date=execution_date,fixed_selection=fixed_selector,maximum_provider_requests=10,
              overall_timeout_seconds=1800,volume_policy_scope="per_symbol",transactions=[],
              requests_attempted=0,budget_debits_confirmed=0,budget_debits_unconfirmed=0,
              published_count=0,last_verified_commit_sha=operational_store_sha,stop=None))
        cursor=anchor=plan_sha
        main_line=list(order)
        successor=dict(zip(main_line,main_line[1:]))
        passed=set(); prefix_rows=[]
        for count,target in enumerate(targets,1):
            symbol=target["symbol"]
            path=part[symbol]
            versions=history.get(path); cp=members.get(checkpoint[symbol])
            if not versions:
                break
            # Accept only the existing exact terminal shape or a stopped child
            # with a single initial report. Neither proves a provider request.
            actions=[action for action,_ in versions]
            _need(actions in (["A"],["A","M","M"]))
            initial=versions[0][1]
            debit=successor.get(initial)
            context=dict(schema_version=1,mode=tail.MODE,compute_sha=compute_sha,store_start_sha=cursor,
                trade_date=execution_date,execution_date=execution_date,historical_cutoff=CUTOFF,
                request_id=request_id,target_count=1,fixed_selection={"schema_version":1,"targets":[target]})
            initial_report=_child_debit(repo,initial,debit,path,budget,cursor,context)
            debits+=1
            anchor=debit
            if actions==["A"]:
                break
            child=_child_terminal(repo,versions[1][1],versions[2][1],debit,initial_report,path,budget,context,symbol)
            _need(order[child]>order[cursor])
            before=native._immutable_budget(repo,cursor,budget)
            budget_obj=_object(repo,child,budget)
            outputs=budget_obj["required_outputs"]
            _need(type(outputs) is list and 0<len(outputs)<=64)
            locators=[]
            for output in outputs:
                _need(type(output) is dict and set(output)=={"path","blob_sha"}
                      and complete.HEX40.fullmatch(output["blob_sha"]) is not None)
                path=output["path"]
                _need(type(path) is str and 0<len(path)<=512 and re.fullmatch(r"[A-Za-z0-9_./-]+",path)
                      and all(x not in ("",".","..") for x in path.split("/")))
                locators.append({"path":native.r.PREFIX+path,"blob_sha":output["blob_sha"]})
            proved=native._verify_append_terminal(h,repo,start=cursor,sha=child,path=part[symbol],
                 day=execution_date,cutoff=CUTOFF,request_id=request_id,symbol=symbol,
                 selector={"schema_version":1,"targets":[target]},before=before,remaining=targets,
                 passed=passed|{symbol},compute_sha=compute_sha,
                 result={"status":"PASS","appended_count":1,"verified_required_outputs":locators})
            s=proved["results"][0]
            _need(s["new_records"]==1 and s["last_trade_date"]==CUTOFF and s["volume_policy"] in complete.VOL)
            anchor=child
            durable.append(s["volume_policy"])
            passed.add(symbol)
            if not cp:
                # No schedule/checkpoint guess. This child itself is durable.
                break
            _need(order[cp]>order[child])
            row=dict(symbol=symbol,status="PASS",store_start_sha=cursor,store_commit_sha=child,
                     result_state_path=part[symbol],volume_policy=s["volume_policy"])
            native._metadata_proof(repo,cp,child,checkpoint[symbol],execution_date,request_id)
            _matches(_object(repo,cp,checkpoint[symbol]),dict(common,mode=native.MODE,trade_date=execution_date,
                kind="append_only_batch_checkpoint",status="PROGRESS_VALIDATED",batch_manifest_commit_sha=plan_sha,
                batch_manifest_path=plan,transactions=prefix_rows+[row],last_verified_commit_sha=child,
                requests_attempted=count,budget_debits_confirmed=count,budget_debits_unconfirmed=0,
                published_count=count,quarantined_symbols=[],stop=None))
            prefix_rows.append(dict(row,checkpoint_commit_sha=cp,checkpoint_path=checkpoint[symbol]))
            checked+=1
            cursor=anchor=cp
    except (Exception,SystemExit):
        pass
    if anchor is not None:
        try:
            # The main view may be a later descendant, not the exact run head.
            # If it advanced during this diagnostic, do not assert an observation.
            native._remote_exact(repo,observed_main)
        except (Exception,SystemExit):
            return facts
        facts.update(verified_price_rows=len(durable),verified_checkpoints=checked,
                     verified_volume_unresolved=durable.count("BLOCKED_UNRESOLVED"),
                     verified_project_credit_debits_lower_bound=debits,
                     prefix_evidence_available=bool(durable or debits),later_main_observed=anchor!=observed_main)
    return facts
