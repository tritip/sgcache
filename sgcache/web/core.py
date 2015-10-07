import json
import logging
import os
import time

from flask import Flask, request, Response, stream_with_context, g, redirect
from werkzeug.http import remove_hop_by_hop_headers
import requests
import sqlalchemy as sa
import yaml

from .. import config
from ..cache import Cache
from ..exceptions import Passthrough, Fault
from ..logs import setup_logs, log_globals
from ..schema import Schema
from ..utils import get_shotgun_kwargs

log = logging.getLogger(__name__)


app = Flask(__name__)
app.config.from_object(config)

db = sa.create_engine(app.config['SQLA_URL'], echo=bool(app.config['SQLA_ECHO']))

# Setup logging *after* SQLA so that it can deal with its handlers.
setup_logs(app)

schema = Schema.from_yaml(app.config['SCHEMA'])
cache = Cache(db, schema) # SQL DDL is executed here; watch out!

# Get the fallback server from shotgun_api3_registry.
FALLBACK_SERVER = get_shotgun_kwargs()['base_url'].strip('/')
FALLBACK_URL = FALLBACK_SERVER + '/api3/json'

# We use one HTTP session for everything.
http_session = requests.Session()


# This is used by our shotgun_api3_registry to assert that the cache is up.
# In the future we may have something with a bit more information, or have the
# "info" method return a bit more.
@app.route('/ping')
def on_ping():
    return 'pong', 200, [('Content-Type', 'text/plain')]


# Forward detail requests through to the real thing.
@app.route('/')
@app.route('/detail/<path:path>')
@app.route('/page/<path:path>')
def forward_details(path=''):
    url = FALLBACK_SERVER + request.path
    return redirect(url)



@app.route('/api3/json', methods=['POST'])
@app.route('/<path:params>/api3/json', methods=['POST'])
def json_api(params=None):

    payload = g.api3_payload = json.loads(request.data)

    if not isinstance(payload, dict):
        return '', 400, []

    try:
        method_name = payload['method_name']
        params = payload['params']
        auth_params = params[0] if params else {}
        method_params = params[1] if len(params) > 1 else {}
    except KeyError:
        return '', 400, []

    # Log the base of the request.
    headline_chunks = ['Starting %s' % method_name]
    entity_type = method_params.get('type')
    if entity_type:
        headline_chunks.append('on %s' % entity_type)
    script_name = auth_params.get('script_name')
    if script_name:
        headline_chunks.append('by script "%s"' % script_name)
        sudo_as_login = auth_params.get('sudo_as_login')
        if sudo_as_login:
            headline_chunks.append('as user "%s"' % sudo_as_login)
    else:
        user_login = auth_params.get('user_login')
        if user_login:
            headline_chunks.append('by user "%s"' % user_login)
    log.info(' '.join(headline_chunks))

    try:
        method = _api3_methods[method_name]
    except KeyError as e:
        log.info('Passing through "%s" due to unknown API method' % method_name)
        return passthrough()

    try:
        start_time = time.time()
        res_data = method(method_params)
        if isinstance(res_data, dict):
            res_tuple = json.dumps(res_data), 200, [('Content-Type', 'application/json')]
        elif isinstance(res_data, tuple):
            res_tuple = res_data
        else:
            raise TypeError('api3 method returned %s' % type(res_data))

    except Fault as e:
        log.warning('%s (%s): %s' % (e.__class__.__name__, e.code, e.args[0]))
        res_data = {
            'exception': True,
            'error_code': e.code,
            'message': e.args[0],
        }
        # Shotgun does still return a 200 here.
        res_tuple = json.dumps(res_data), 200, [('Content-Type', 'application/json')]

    except Passthrough as e:
        log.info('Passing through %s due to %s("%s"):%s%s' % (
            method_name,
            e.__class__.__name__,
            e,
            '\n' if method_params else '',
            json.dumps(method_params or {}, sort_keys=True, indent=4) if method_params else '',
        ))
        res_data = {}
        res_tuple = passthrough()

    elapsed_ms = 1000 * (time.time() - start_time)
    log.info('Returned %sin %.1fms' % (
        '%s %ss ' % (len(res_data['entities']), entity_type) if 'entities' in res_data else '',
        elapsed_ms
    ))
    log_globals.skip_http_log = True

    return res_tuple



def passthrough():

    # our "Host" is different than theirs
    headers = dict(request.headers)
    headers.pop('Host')

    res = http_session.post(FALLBACK_URL, data=request.data, headers=headers, stream=True)

    if res.status_code == 200:
        return Response(stream_with_context(_process_passthrough_response(res)), mimetype='application/json')
    else:
        return res.text, res.status_code, [('Content-Type', 'application/json')]


def _process_passthrough_response(res):
    buffer_ = []
    for chunk in res.iter_content(8192):
        yield chunk
        buffer_.append(chunk)
    # TODO: analyze it here


# For handing a Flask stream to Requests; the iter API is likely
# throwing Requests off.
class _StreamReadWrapper(object):
    def __init__(self, fh):
        self.read = fh.read

@app.route('/file_serve/<path:path>', methods=['GET', 'POST'])
@app.route('/thumbnail/<path:path>', methods=['GET', 'POST'])
@app.route('/upload/<path:path>', methods=['GET', 'POST'])
def proxy(path):

    url = FALLBACK_SERVER + request.path

    # Strip out the hop-by-hop headers, AND the host (since that likely points
    # to the cache and not Shotgun).
    headers = [(k, v) for k, v in request.headers.items() if k.lower() != 'host']
    remove_hop_by_hop_headers(headers)

    remote_response = http_session.request(request.method, url,
        data=_StreamReadWrapper(request.stream),
        params=request.args,
        headers=dict(headers),
        stream=True,
    )

    headers = remote_response.headers.items()
    remove_hop_by_hop_headers(headers)

    return Response(
        remote_response.iter_content(8192),
        status=remote_response.status_code,
        headers=headers,
        direct_passthrough=True, # Don't encode it.
    )



# Register api methods
_api3_methods = {}
def api3_method(func):
    _api3_methods[func.__name__] = func
    return func
from . import api3
