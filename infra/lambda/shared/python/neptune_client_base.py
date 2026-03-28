"""
neptune_client_base.py - Shared Neptune Gremlin client utilities.

This module is deployed as a Lambda Layer (neptune-client-base) and is
available to all ETL Lambda functions at /opt/python/neptune_client_base.py.

Reads NEPTUNE_ENDPOINT, NEPTUNE_PORT, REGION from environment variables.
"""

import os
import json
import logging
import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger()

NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', 'YOUR_NEPTUNE_ENDPOINT')
NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
REGION = os.environ.get('REGION', 'YOUR_AWS_REGION')

_frozen_creds = None
_http_session = None


def _get_creds():
    global _frozen_creds
    if _frozen_creds is None:
        _frozen_creds = boto3.Session(region_name=REGION).get_credentials().get_frozen_credentials()
    return _frozen_creds


def _get_http_session():
    global _http_session
    if _http_session is None:
        import requests as req_lib
        _http_session = req_lib.Session()
    return _http_session


def neptune_query(gremlin: str) -> dict:
    creds = _get_creds()
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
    data = json.dumps({"gremlin": gremlin})
    headers = {
        "Content-Type": "application/json",
        "host": f"{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}",
    }
    aws_req = AWSRequest(method="POST", url=url, data=data, headers=headers)
    SigV4Auth(creds, "neptune-db", REGION).add_auth(aws_req)
    r = _get_http_session().post(url, headers=dict(aws_req.headers), data=data, verify=False, timeout=20)
    if r.status_code != 200:
        raise Exception(f"Neptune error {r.status_code}: {r.text[:300]}")
    return r.json()


def safe_str(s) -> str:
    return str(s).replace("'", "\\'").replace('"', '\\"')[:256]


def extract_value(val):
    if isinstance(val, dict) and '@value' in val:
        v = val['@value']
        if isinstance(v, list) and len(v) > 0:
            return extract_value(v[0])
        return v
    return val
