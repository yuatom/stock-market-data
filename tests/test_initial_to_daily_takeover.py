from initial_to_daily_hosted_fixture import run


def test_same_store_actual_initial_read_then_hosted_daily_capture_and_fresh_read(tmp_path):
    data=run(tmp_path/'chain')
    assert data['initial']['grant']['selector']['basis_id']!=data['daily']['candidate']['selector']['basis_id']
    assert data['initial_rejected_after_takeover']
    assert data['synthetic_provider_callbacks']==data['synthetic_dispatch_callbacks']==1
    assert data['external_provider_requests']==data['external_dispatches']==0
    assert 'initial-read-clone' in data['clone_names'] and 'standalone-reader' in data['clone_names']
