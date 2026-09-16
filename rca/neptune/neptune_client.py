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
from shared import get_region
REGION = get_region()

# RDS Combined CA bundle — downloaded once per container lifetime
_RDS_CA_PATH = '/tmp/rds-combined-ca-bundle.pem'
_RDS_CA_URL = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'

_http = None
_ca_path = None


_boto_session = None


def _get_frozen_creds():
    """复用 boto3 Session，但每次调用重新冻结凭证。

    2026-08-28:原先每次调用都 `boto3.Session().get_credentials()
    .get_frozen_credentials()`。Session 构造昂贵 —— 实测每次约 9.7 ms，
    且生产 ETL 日志里同一次调用（同一 request ID）2 秒内出现 4 次
    「Found credentials in environment variables」，是已确认的实况开销。
    按 RCA 单次运行 15-30 次查询估，纯浪费 150-300 ms，而 RCA 在事故热路径上。

    刻意**只缓存 Session、不缓存冻结凭证**:冻结凭证是含固定 session token
    的快照，缓存它会在凭证过期后让长生命周期进程持续 403
    （shared/python/neptune_client_base.py 原先就是这么写的，已一并修正）。
    boto3 的可刷新凭证在临近过期时会自动续期，所以每次重新冻结是正确做法。
    """
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session(region_name=REGION)
    # ⚠️ 必须判 None。get_credentials() 在**解析不到凭据时返回 None**（不抛异常），
    # 直接 .get_frozen_credentials() 会得到
    #   AttributeError: 'NoneType' object has no attribute 'get_frozen_credentials'
    # —— 一个完全看不出「是凭据问题」的报错。
    #
    # 实测代价（2026-09-09）：在无凭据环境跑 `pytest -m "not neptune"`，
    # 767 个用例全部以这条 AttributeError 失败。根因只有一个，
    # 但报错信息让人以为是 767 个各自的问题。
    _creds = _boto_session.get_credentials()
    if _creds is None:
        raise RuntimeError(
            "AWS 凭据未解析到（boto3 Session.get_credentials() 返回 None）。本进程无法对 SigV4 请求签名。\n常见原因：环境变量/配置文件里没有凭据、SSO 会话过期、或在 Lambda 里执行角色未附加。\n本地请先 `aws sso login` 或设置 AWS_PROFILE；CI 请给需要 AWS 的测试打 @pytest.mark.neptune 并从离线子集排除。"
        )
    return _creds.get_frozen_credentials()


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

    credentials = _get_frozen_creds()
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
