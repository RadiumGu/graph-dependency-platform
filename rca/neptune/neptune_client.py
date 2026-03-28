"""
neptune_client.py - Neptune 查询封装
复用 etl_deepflow 的 SigV4 模式
"""
import os
import json
import logging
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)

NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', '')
NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
REGION = os.environ.get('REGION', 'ap-northeast-1')

# RDS Combined CA bundle — downloaded once per container lifetime
_RDS_CA_PATH = '/tmp/rds-combined-ca-bundle.pem'
_RDS_CA_URL = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'

_http = None
_ca_path = None


def _get_ca_path() -> str:
    """Return path to the RDS CA bundle, downloading it on first use."""
    global _ca_path
    if _ca_path:
        return _ca_path
    if os.path.exists(_RDS_CA_PATH):
        _ca_path = _RDS_CA_PATH
        return _ca_path
    try:
        import urllib.request
        urllib.request.urlretrieve(_RDS_CA_URL, _RDS_CA_PATH)
        _ca_path = _RDS_CA_PATH
        logger.info('RDS CA bundle downloaded to %s', _RDS_CA_PATH)
    except Exception as e:
        logger.warning('Failed to download RDS CA bundle: %s — SSL verification disabled', e)
        _ca_path = False  # type: ignore[assignment]
    return _ca_path


def _get_http():
    global _http
    if _http is None:
        import requests
        _http = requests.Session()
    return _http

def query(cypher: str, params: dict = None) -> dict:
    """执行 openCypher 查询，返回 response JSON"""
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"
    body_dict = {"query": cypher}
    if params:
        body_dict["parameters"] = json.dumps(params)
    body = json.dumps(body_dict).encode()

    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method='POST', url=url, data=body,
                         headers={'Content-Type': 'application/json'})
    SigV4Auth(credentials, 'neptune-db', REGION).add_auth(request)

    # Neptune uses IAM auth (SigV4), SSL verification with RDS CA bundle
    # may fail in Lambda environments. Fall back to verify=False if CA unavailable.
    resp = _get_http().post(url, data=body,
                            headers=dict(request.headers), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()

def results(cypher: str, params: dict = None) -> list:
    """返回 results 列表，简化调用"""
    data = query(cypher, params)
    return data.get('results', [])
