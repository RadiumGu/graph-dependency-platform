"""
eks_auth.py - Shared EKS authentication helpers

Centralises EKS token generation so action_executor.py and
infra_collector.py use the same implementation instead of maintaining
separate copies with slightly different signing approaches.
"""
import base64
import logging
import os
import tempfile

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger(__name__)

REGION = os.environ.get('REGION', 'ap-northeast-1')


def get_k8s_endpoint(cluster_name: str) -> tuple[str, str]:
    """
    Return (endpoint, ca_data) for the given EKS cluster.

    ca_data is the base64-encoded certificate authority data string
    returned by the EKS API; use _write_ca() to materialise it as a
    file path suitable for TLS verification.
    """
    eks = boto3.client('eks', region_name=REGION)
    cluster = eks.describe_cluster(name=cluster_name)['cluster']
    return cluster['endpoint'], cluster['certificateAuthority']['data']


def get_eks_token(cluster_name: str) -> str:
    """
    Generate a bearer token equivalent to `aws eks get-token`.

    Uses a SigV4-signed presigned STS GetCallerIdentity URL with the
    cluster name injected as the x-k8s-aws-id header.
    """
    # ⚠️ 判 None 的理由同 neptune_client._get_frozen_creds：
    # get_credentials() 解析不到凭据时返回 None 而不抛异常。
    _c = boto3.Session().get_credentials()
    if _c is None:
        raise RuntimeError(
            "AWS 凭据未解析到（boto3 Session.get_credentials() 返回 None）。本进程无法对 SigV4 请求签名。\n常见原因：环境变量/配置文件里没有凭据、SSO 会话过期、或在 Lambda 里执行角色未附加。\n本地请先 `aws sso login` 或设置 AWS_PROFILE；CI 请给需要 AWS 的测试打 @pytest.mark.neptune 并从离线子集排除。"
        )
    creds = _c.get_frozen_credentials()
    url = f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15'
    headers = {'x-k8s-aws-id': cluster_name}
    request = AWSRequest(method='GET', url=url, headers=headers)
    SigV4QueryAuth(creds, 'sts', REGION, expires=60).add_auth(request)
    return 'k8s-aws-v1.' + base64.urlsafe_b64encode(
        request.url.encode()
    ).decode().rstrip('=')


def write_ca(ca_data: str) -> str:
    """
    Decode base64 CA data and write it to a temporary file.

    Returns the file path, which can be passed to the kubernetes client
    or an SSL context as the CA bundle.
    """
    ca_bytes = base64.b64decode(ca_data)
    f = tempfile.NamedTemporaryFile(delete=False, suffix='.crt')
    f.write(ca_bytes)
    f.close()
    return f.name
