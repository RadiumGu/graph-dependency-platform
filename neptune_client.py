"""
neptune_client.py - Neptune 查询封装
复用 etl_deepflow 的 SigV4 模式
"""
import os
import json
import logging
import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT',
    'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
REGION = os.environ.get('REGION', 'ap-northeast-1')

_http = None

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

    resp = _get_http().post(url, data=body,
                            headers=dict(request.headers), verify=False, timeout=10)
    resp.raise_for_status()
    return resp.json()

def results(cypher: str, params: dict = None) -> list:
    """返回 results 列表，简化调用"""
    data = query(cypher, params)
    return data.get('results', [])
