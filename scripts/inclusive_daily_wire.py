"""Pure hosted-route wire mapping. Logical approval and row bounds stay dates.

Twelve Data date-only end_date can exclude that session. Daily hosted capture
uses the same approved end date at 23:59:59, never a next-day upper bound.
No credentials, transport, permission, retries or Store operations live here.
"""
from datetime import date
import re

REQUEST_KEYS=frozenset('provider symbol interval exchange mic_code currency type adjust dp start_date end_date outputsize order'.split())
PARAMETER_KEYS=('symbol','interval','exchange','mic_code','type','adjust','dp','start_date','end_date','outputsize','order')
ENDPOINT='https://api.twelvedata.com/time_series'


def daily_wire_parameters(request):
    """Return secret-free query parameters without mutating the logical request."""
    try:
        if type(request) is not dict or set(request)!=REQUEST_KEYS:
            raise ValueError()
        if request['provider']!='twelve_data' or request['interval']!='1day':
            raise ValueError()
        for name in ('start_date','end_date'):
            value=request[name]
            if type(value) is not str or not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',value):
                raise ValueError()
            if date.fromisoformat(value).isoformat()!=value:
                raise ValueError()
        if request['start_date']>request['end_date']:
            raise ValueError()
        params={name:request[name] for name in PARAMETER_KEYS}
        params['end_date']=request['end_date']+' 23:59:59'
        params['format']='JSON'
        return params
    except Exception:
        pass
    raise ValueError('INVALID_DAILY_WIRE_REQUEST')


def daily_wire_request_plan(request):
    """Private pre-request intent, not proof that HTTP happened or completed."""
    return dict(schema_version=1,kind='provider_wire_request_plan',endpoint=ENDPOINT,
        parameters=daily_wire_parameters(request),credential_omitted=True,
        boundary_policy='inclusive_date_same_day_235959')
