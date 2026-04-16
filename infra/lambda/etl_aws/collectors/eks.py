"""
collectors/eks.py - EKS cluster, nodegroups, K8s services, pods collectors.
"""

import base64
import ssl
import logging
import urllib.request as _ureq
import json as _json

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from config import EKS_CLUSTER_NAME, REGION

logger = logging.getLogger()

# Local alias map (same as K8S_SERVICE_ALIAS in config but also used inline)
_K8S_SVC_ALIAS = {
    'pay-for-adoption':  'payforadoption',
    'list-adoptions':    'petlistadoptions',
    'search-service':    'petsearch',
    'pethistory':        'pethistory',
    'petsite':           'petsite',
    'traffic-generator': 'trafficgenerator',
}


def _get_eks_token(cluster_name: str = None) -> tuple:
    """Generate EKS bearer token and return (endpoint, token)."""
    cluster_name = cluster_name or EKS_CLUSTER_NAME
    try:
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        signer = SigV4QueryAuth(
            credentials=Credentials(creds.access_key, creds.secret_key, creds.token),
            service_name='sts', region_name=REGION, expires=60,
        )
        url = f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15'
        req = AWSRequest(method='GET', url=url, headers={'x-k8s-aws-id': cluster_name})
        signer.add_auth(req)
        token = 'k8s-aws-v1.' + base64.urlsafe_b64encode(req.url.encode()).decode().rstrip('=')
        return token
    except Exception as e:
        logger.warning(f"EKS token generation failed: {e}")
        return None


def _ssl_ctx_no_verify():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def collect_eks_cluster(eks_client) -> dict:
    try:
        resp = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)
        cluster = resp['cluster']
        return {
            'name': cluster['name'],
            'version': cluster.get('version', ''),
            'status': cluster.get('status', ''),
            'endpoint': cluster.get('endpoint', ''),
            'arn': cluster.get('arn', ''),
        }
    except Exception as e:
        logger.error(f"EKS cluster describe failed: {e}")
        return {}


def collect_eks_nodegroup_instances(eks_client, ec2_client) -> list:
    members = []
    try:
        ngs = eks_client.list_nodegroups(clusterName=EKS_CLUSTER_NAME)
        for ng_name in ngs.get('nodegroups', []):
            ng = eks_client.describe_nodegroup(
                clusterName=EKS_CLUSTER_NAME, nodegroupName=ng_name
            )
            asg_names = [
                res['name']
                for res in ng['nodegroup'].get('resources', {}).get('autoScalingGroups', [])
            ]
            if asg_names:
                asg_client = boto3.client('autoscaling', region_name=REGION)
                for asg_name in asg_names:
                    resp = asg_client.describe_auto_scaling_groups(
                        AutoScalingGroupNames=[asg_name]
                    )
                    for asg in resp.get('AutoScalingGroups', []):
                        for inst in asg.get('Instances', []):
                            members.append(inst['InstanceId'])
    except Exception as e:
        logger.error(f"EKS nodegroup instances failed: {e}")
    logger.info(f"EKS member instances: {members}")
    return members


def collect_k8s_services(eks_client) -> list:
    """Collect K8s Services from all namespaces via K8s API."""
    try:
        token = _get_eks_token()
        if not token:
            return []
        cluster_info = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)['cluster']
        endpoint = cluster_info['endpoint']
        ctx = _ssl_ctx_no_verify()
        api_req = _ureq.Request(
            f'{endpoint}/api/v1/services',
            headers={'Authorization': f'Bearer {token}'}
        )
        with _ureq.urlopen(api_req, context=ctx, timeout=10) as resp:
            data = _json.loads(resp.read())

        services = []
        for s in data.get('items', []):
            meta = s['metadata']
            spec = s.get('spec', {})
            sel  = spec.get('selector', {})
            svc_name = meta['name']
            app_label = sel.get('app', sel.get('app.kubernetes.io/name', ''))
            ms_alias  = _K8S_SVC_ALIAS.get(app_label, app_label)
            services.append({
                'name':       svc_name,
                'namespace':  meta.get('namespace', 'default'),
                'type':       spec.get('type', 'ClusterIP'),
                'cluster_ip': spec.get('clusterIP', ''),
                'app_label':  app_label,
                'ms_alias':   ms_alias,
            })
        logger.info(f"K8s Services (all ns): {len(services)}")
        return services
    except Exception as e:
        logger.warning(f"collect_k8s_services: {e}")
        return []


def get_pod_ip_to_app_label(eks_client) -> dict:
    """Return {pod_ip: app_label} for TG healthy target → Microservice mapping."""
    try:
        token = _get_eks_token()
        if not token:
            return {}
        cluster_info = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)['cluster']
        endpoint = cluster_info['endpoint']
        ctx = _ssl_ctx_no_verify()
        api_req = _ureq.Request(
            f'{endpoint}/api/v1/pods',
            headers={'Authorization': f'Bearer {token}'}
        )
        with _ureq.urlopen(api_req, context=ctx, timeout=10) as resp:
            data = _json.loads(resp.read())
        result = {}
        for item in data.get('items', []):
            pod_ip = item.get('status', {}).get('podIP', '')
            labels = item.get('metadata', {}).get('labels', {})
            app_label = labels.get('app') or labels.get('app.kubernetes.io/name', '')
            if pod_ip and app_label:
                result[pod_ip] = app_label
        logger.info(f"pod_ip_to_app_label: {len(result)} entries")
        return result
    except Exception as e:
        logger.warning(f"get_pod_ip_to_app_label: {e}")
        return {}


def collect_eks_pods(eks_client, ec2_client) -> list:
    """Collect all EKS pods with AZ info."""
    token = _get_eks_token()
    if not token:
        return []
    try:
        cluster_info = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)['cluster']
        endpoint = cluster_info['endpoint']
    except Exception as e:
        logger.warning(f"EKS describe cluster for pods: {e}")
        return []

    ctx = _ssl_ctx_no_verify()
    try:
        req = _ureq.Request(
            f'{endpoint}/api/v1/pods',
            headers={'Authorization': f'Bearer {token}'}
        )
        with _ureq.urlopen(req, context=ctx, timeout=10) as resp:
            pod_data = _json.loads(resp.read())
    except Exception as e:
        logger.warning(f"K8s pods list failed: {e}")
        return []

    node_names = list(set(
        item['spec'].get('nodeName', '')
        for item in pod_data.get('items', [])
        if item['spec'].get('nodeName')
    ))
    node_az_map = {}
    if node_names:
        try:
            resp_ec2 = ec2_client.describe_instances(
                Filters=[{'Name': 'private-dns-name', 'Values': node_names}]
            )
            for r in resp_ec2['Reservations']:
                for inst in r['Instances']:
                    node_az_map[inst['PrivateDnsName']] = inst['Placement']['AvailabilityZone']
        except Exception as e:
            logger.warning(f"Node AZ lookup failed: {e}")

    pods = []
    for item in pod_data.get('items', []):
        meta = item['metadata']
        spec = item['spec']
        status = item['status']
        name = meta['name']
        namespace = meta.get('namespace', 'default')
        labels = meta.get('labels', {})
        service_name = labels.get('app', labels.get('app.kubernetes.io/name', ''))
        phase = status.get('phase', 'Unknown')
        node = spec.get('nodeName', '')
        az = node_az_map.get(node, '')
        restarts, reason = 0, ''
        for cs in status.get('containerStatuses', []):
            restarts = max(restarts, cs.get('restartCount', 0))
            waiting = cs.get('state', {}).get('waiting', {})
            if waiting.get('reason'):
                reason = waiting['reason']
        pods.append({
            'name': name,
            'namespace': namespace,
            'service_name': service_name,
            'status': phase,
            'restarts': restarts,
            'reason': reason,
            'node_name': node,
            'az': az,
            'pod_ip': status.get('podIP', ''),
        })
    logger.info(f"EKS pods collected: {len(pods)}")
    return pods


def collect_k8s_deployments(eks_client) -> list:
    """Collect K8s Deployments from all namespaces via K8s API."""
    try:
        token = _get_eks_token()
        if not token:
            return []
        cluster_info = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)['cluster']
        endpoint = cluster_info['endpoint']
        ctx = _ssl_ctx_no_verify()
        api_req = _ureq.Request(
            f'{endpoint}/apis/apps/v1/deployments',
            headers={'Authorization': f'Bearer {token}'}
        )
        with _ureq.urlopen(api_req, context=ctx, timeout=10) as resp:
            data = _json.loads(resp.read())

        SKIP_NS = {'kube-system', 'kube-public', 'kube-node-lease',
                   'amazon-cloudwatch', 'amazon-guardduty', 'cert-manager',
                   'chaos-mesh', 'deepflow'}
        deployments = []
        for d in data.get('items', []):
            meta = d['metadata']
            ns = meta.get('namespace', 'default')
            if ns in SKIP_NS:
                continue
            spec = d.get('spec', {})
            status = d.get('status', {})
            labels = spec.get('selector', {}).get('matchLabels', {})
            app_label = labels.get('app', labels.get('app.kubernetes.io/name', ''))
            ms_alias = _K8S_SVC_ALIAS.get(app_label, app_label)
            deployments.append({
                'name':             meta['name'],
                'namespace':        ns,
                'app_label':        app_label,
                'ms_alias':         ms_alias,
                'replicas':         spec.get('replicas', 1),
                'ready_replicas':   status.get('readyReplicas', 0),
                'updated_replicas': status.get('updatedReplicas', 0),
                'available':        status.get('availableReplicas', 0),
                'strategy':         spec.get('strategy', {}).get('type', 'RollingUpdate'),
            })
        logger.info(f"K8s Deployments collected: {len(deployments)}")
        return deployments
    except Exception as e:
        logger.warning(f"collect_k8s_deployments: {e}")
        return []
