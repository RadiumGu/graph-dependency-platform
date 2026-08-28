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

_boto_session = None
_http_session = None


def _get_creds():
    """取 SigV4 凭证。

    2026-08-28 修正。原实现把 `get_frozen_credentials()` 的**快照**永久缓存在
    模块全局:

        _frozen_creds = None
        def _get_creds():
            global _frozen_creds
            if _frozen_creds is None:
                _frozen_creds = ...get_frozen_credentials()
            return _frozen_creds

    `get_frozen_credentials()` 返回的是不可变快照，含固定的 session token。
    Lambda 容器可被复用数小时，而执行角色凭证有有效期 —— 过期后该热容器的
    每次 Neptune 调用都会 403，直到容器被回收。

    如实说明:近 7 天 ETL 日志里**没有**观测到 403 / ExpiredToken，
    所以这是「明确写错但在观测窗口内尚未触发」的潜伏缺陷，
    触发与否取决于容器存活是否超过凭证有效期。

    正确做法是缓存 **Session**（构造昂贵）而每次调用重新取冻结凭证
    （boto3 的可刷新凭证会在临近过期时自动续期）。
    顺带这也修掉一个已确认的实况开销:原实现每次调用都
    `boto3.Session(...)`，生产日志里同一次调用（同一 request ID）2 秒内
    出现 4 次「Found credentials in environment variables」——
    实测每次新建 Session + 取凭证约 9.7 ms。
    """
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session(region_name=REGION)
    # 每次重新冻结，让可刷新凭证有机会续期
    return _boto_session.get_credentials().get_frozen_credentials()


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
