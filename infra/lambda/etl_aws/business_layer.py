"""
business_layer.py - BusinessCapability nodes and ECR startup dependency scanning.
"""

import base64
import logging
import time
import boto3

from neptune_client import (
    neptune_query, upsert_vertex, upsert_edge, safe_str, extract_value,
)
from config import (
    BUSINESS_CAPABILITIES, MICROSERVICE_RECOVERY_PRIORITY,
    MICROSERVICE_NAMESPACE, SERVICE_TYPES,
    K8S_SERVICE_ALIAS, EKS_CLUSTER_NAME, REGION,
)

logger = logging.getLogger()


def upsert_business_capabilities() -> dict:
    """
    Upsert BusinessCapability nodes and connect them to technical services.

    Edges:
      Microservice    -[Implements]-> BusinessCapability
      LambdaFunction  -[Implements]-> BusinessCapability
      BusinessCapability -[DependsOn]-> infrastructure (non-standard infra only)
    """
    stats = {'created': 0, 'edges': 0}
    ts = int(time.time())

    for cap in BUSINESS_CAPABILITIES:
        cap_name = safe_str(cap['name'])
        priority = safe_str(cap['recovery_priority'])
        desc = safe_str(cap['description'])

        cap_vid = upsert_vertex('BusinessCapability', cap_name, {
            'description': desc,
            'recovery_priority': priority,
            'layer': 'business',
        }, 'manual')
        if not cap_vid:
            logger.warning(f"BusinessCapability upsert failed: {cap_name}")
            continue
        stats['created'] += 1

        SKIP_BC_INFRA_LABELS = {'RDSCluster', 'DynamoDBTable', 'SQSQueue', 'SNSTopic', 'StepFunction', 'S3Bucket'}
        INTERNAL_KEYWORDS = ('provider', 'waiter', 'framework', 'customresource',
                              'arn:aws:', 'iscompl', 'onevent', 'ontimeout')

        for dep_label in cap.get('depends_on_types', []):
            if dep_label in SKIP_BC_INFRA_LABELS:
                continue
            dl = safe_str(dep_label)
            try:
                r_nodes = neptune_query(
                    f"g.V().hasLabel('{dl}').project('id','name').by(id()).by(values('name')).toList()"
                )
                node_list = r_nodes.get('result', {}).get('data', {}).get('@value', [])
                for node_item in node_list:
                    node_vals = {}
                    nv = node_item.get('@value', []) if isinstance(node_item, dict) else []
                    for i in range(0, len(nv), 2):
                        k = nv[i]; v = nv[i+1]
                        if isinstance(v, dict) and '@value' in v:
                            vl = v['@value']; v = vl[0] if vl else ''
                            if isinstance(v, dict) and '@value' in v: v = v['@value']
                        node_vals[k] = str(v)
                    node_id = node_vals.get('id', '')
                    node_name = node_vals.get('name', '').lower()
                    if any(kw in node_name for kw in INTERNAL_KEYWORDS):
                        continue
                    if not node_id:
                        continue
                    try:
                        neptune_query(
                            f"g.V('{cap_vid}').as('cap').V('{node_id}')"
                            f".coalesce("
                            f"  __.inE('DependsOn').where(__.outV().hasId('{cap_vid}')),"
                            f"  __.addE('DependsOn').from('cap')"
                            f").property('source','business-layer')"
                            f".property('phase','runtime')"
                            f".property('strength','strong')"
                            f".property('last_updated',{ts})"
                        )
                        stats['edges'] += 1
                    except Exception as e:
                        logger.debug(f"DependsOn edge {cap_name}→{node_name}: {e}")
            except Exception as e:
                logger.warning(f"DependsOn query {cap_name}→{dep_label}: {e}")

        for svc_name in cap.get('serves_services', []):
            sn = safe_str(svc_name)
            svc_priority = MICROSERVICE_RECOVERY_PRIORITY.get(svc_name, cap.get('recovery_priority', 'Tier2'))
            _svc_type = SERVICE_TYPES.get(svc_name, 'k8s')
            svc_vid = upsert_vertex('Microservice', sn, {
                'namespace': MICROSERVICE_NAMESPACE.get(svc_name, 'default'),
                'source': 'business-layer',
                'fault_boundary': 'region' if _svc_type == 'lambda' else 'az',
                'region': REGION,
                'recovery_priority': svc_priority,
                'service_type': _svc_type,
                'log_source': f"cwlogs:///aws/containerinsights/{EKS_CLUSTER_NAME}/application?filter={sn}",
            }, 'manual')
            if not svc_vid:
                continue
            try:
                neptune_query(
                    f"g.V('{svc_vid}').as('svc')"
                    f".V('{cap_vid}')"
                    f".coalesce("
                    f"  __.inE('Implements').where(__.outV().hasId('{svc_vid}')),"
                    f"  __.addE('Implements').from('svc')"
                    f").property('source','business-layer').property('last_updated',{ts})"
                )
                stats['edges'] += 1
            except Exception as e:
                logger.debug(f"Implements edge Microservice({svc_name})→{cap_name} skip: {e}")

        for fn_pattern in cap.get('serves_lambda', []):
            fp = safe_str(fn_pattern)
            try:
                neptune_query(
                    f"g.V().hasLabel('LambdaFunction').has('name', containing('{fp}')).as('fn')"
                    f".V('{cap_vid}')"
                    f".coalesce("
                    f"  __.inE('Implements').where(__.outV().hasLabel('LambdaFunction').has('name',containing('{fp}'))),"
                    f"  __.addE('Implements').from('fn')"
                    f").property('source','business-layer').property('last_updated',{ts})"
                )
                stats['edges'] += 1
            except Exception as e:
                logger.debug(f"Implements edge Lambda({fn_pattern})→{cap_name} skip: {e}")

    logger.info(f"BusinessCapability: created={stats['created']}, edges={stats['edges']}")
    return stats


def _get_eks_token_full(cluster_name: str, session) -> str:
    """Generate EKS Kubernetes API bearer token using STS presigned URL."""
    try:
        import botocore
        from botocore.signers import RequestSigner
        signer = RequestSigner(
            botocore.model.ServiceId('sts'), REGION, 'sts', 'v4',
            session.get_credentials(), session.events,
        )
        params = {
            'method': 'GET',
            'url': f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15',
            'body': {}, 'headers': {'x-k8s-aws-id': cluster_name}, 'context': {},
        }
        signed = signer.generate_presigned_url(params, region_name=REGION, expires_in=60, operation_name='')
        token = 'k8s-aws-v1.' + base64.urlsafe_b64encode(signed.encode()).rstrip(b'=').decode()
        return token
    except Exception as e:
        logger.warning(f"EKS token failed: {e}")
        return ''


def scan_ecr_startup_deps(eks_client, session) -> int:
    """
    Scan EKS pod container images, extract ECR dependencies, write
    DependsOn(phase=startup) edges. Returns number of edges written.
    """
    count = 0
    try:
        cluster_info = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)
        k8s_endpoint = cluster_info['cluster']['endpoint']
        ca_data = cluster_info['cluster']['certificateAuthority']['data']
        token = _get_eks_token_full(EKS_CLUSTER_NAME, session)
        if not token:
            logger.warning("scan_ecr_startup_deps: EKS token unavailable")
            return 0

        import base64 as b64, tempfile, requests as req_lib
        ca_bytes = b64.b64decode(ca_data)
        with tempfile.NamedTemporaryFile(suffix='.crt', delete=False) as f:
            f.write(ca_bytes)
            ca_file = f.name

        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        resp = req_lib.get(
            f'{k8s_endpoint}/api/v1/pods?fieldSelector=metadata.namespace%3Ddefault',
            headers=headers, verify=ca_file, timeout=3
        )
        if resp.status_code != 200:
            logger.warning(f"K8s pods API {resp.status_code}")
            return 0

        ecr_suffix = '.dkr.ecr.' + REGION + '.amazonaws.com/'
        svc_ecr_map: dict = {}
        for pod in resp.json().get('items', []):
            labels = pod.get('metadata', {}).get('labels', {})
            app_label = (labels.get('app') or labels.get('app.kubernetes.io/name') or
                         labels.get('name') or '')
            svc_name = K8S_SERVICE_ALIAS.get(app_label, app_label) if app_label else ''
            if not svc_name:
                continue
            for container in (pod.get('spec', {}).get('containers', []) +
                               pod.get('spec', {}).get('initContainers', [])):
                image = container.get('image', '')
                if ecr_suffix in image:
                    repo_part = image.split(ecr_suffix)[-1].split(':')[0].split('@')[0]
                    if svc_name not in svc_ecr_map:
                        svc_ecr_map[svc_name] = set()
                    svc_ecr_map[svc_name].add(repo_part)

        logger.info(f"ECR startup deps: {len(svc_ecr_map)} services have ECR deps")

        ts = int(time.time())
        for svc_name, ecr_repos in svc_ecr_map.items():
            svc_vids = neptune_query(
                f"g.V().hasLabel('Microservice').has('name','{safe_str(svc_name)}').id().fold()"
            )['result']['data']['@value']
            if not svc_vids or not svc_vids[0].get('@value'):
                continue
            svc_vid = svc_vids[0]['@value'][0]
            if isinstance(svc_vid, dict): svc_vid = svc_vid.get('@value', svc_vid)

            for repo in ecr_repos:
                ecr_vids = neptune_query(
                    f"g.V().hasLabel('ECRRepository').has('name','{safe_str(repo)}').id().fold()"
                )['result']['data']['@value']
                if not ecr_vids or not ecr_vids[0].get('@value'):
                    logger.debug(f"ECR repo not found in graph: {repo}")
                    continue
                ecr_vid = ecr_vids[0]['@value'][0]
                if isinstance(ecr_vid, dict): ecr_vid = ecr_vid.get('@value', ecr_vid)

                neptune_query(
                    f"g.V('{svc_vid}').as('s').V('{ecr_vid}')"
                    f".coalesce("
                    f"  __.inE('DependsOn').where(__.outV().hasId('{svc_vid}')),"
                    f"  __.addE('DependsOn').from('s')"
                    f").property('source','aws-etl')"
                    f".property('phase','startup')"
                    f".property('strength','strong')"
                    f".property('last_updated',{ts})"
                )
                count += 1

    except Exception as e:
        logger.warning(f"scan_ecr_startup_deps failed (non-fatal): {e}", exc_info=True)
    return count
