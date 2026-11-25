"""Explicit collector/publisher wiring candidate; not workflow activation.

Uses the existing invocation/selection logic. The caller must supply the real
owner factory and corresponding stage publisher together. No fallback after a
partially published guarded stage, and no new request authority/state service.
"""
import collect_market_data as entry


def execute(args,*,legacy_publish=None,owner_factory=None,stage_publish=None):
    args=entry.normalize_mode_args(args)
    mode=entry._arg_value(args,'--mode')
    if mode not in entry.runtime.FIXED_GUARDED_MODES:
        # Unsupported modes keep their existing complete legacy route. Never
        # inject an owner or report that an unguarded collector was guarded.
        if legacy_publish is None:raise ValueError('unsupported guarded mode; legacy publisher required')
        result=entry.cli(args)
        if result==0:legacy_publish(args)
        return result
    if owner_factory is None:
        if stage_publish is not None:raise ValueError('stage publisher requires owner factory')
        if legacy_publish is None:raise ValueError('legacy publisher missing')
        result=entry.cli(args)
        if result==0:legacy_publish(args)
        return result
    if stage_publish is None:raise ValueError('guarded publisher must be supplied before collection')
    # Select before any collector operation. Exceptions propagate; never invoke
    # the legacy broad add/pull/rebase path as fallback or second publication.
    return entry.cli(args,owner_factory=owner_factory,stage_publish=stage_publish)
