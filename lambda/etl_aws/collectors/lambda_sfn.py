"""
collectors/lambda_sfn.py - Lambda functions and Step Functions collectors.
"""

import json
import logging
from neptune_client import resolve_managed_by_dict, resolve_resource_tags
from config import CDK_LAMBDA_SKIP_PREFIXES, CDK_LAMBDA_SKIP_KEYWORDS

logger = logging.getLogger()


def collect_lambda_functions(lambda_client) -> list:
    fns = []
    paginator = lambda_client.get_paginator('list_functions')
    for page in paginator.paginate():
        for fn in page['Functions']:
            fn_name = fn['FunctionName']
            if any(fn_name.startswith(p) for p in CDK_LAMBDA_SKIP_PREFIXES):
                continue
            if any(kw.lower() in fn_name.lower() for kw in CDK_LAMBDA_SKIP_KEYWORDS):
                continue
            fn_arn = fn['FunctionArn']
            try:
                tags_resp = lambda_client.list_tags(Resource=fn_arn)
                tags_dict = tags_resp.get('Tags', {})
                rtags = resolve_resource_tags(tags_dict)
                managed_by = rtags.get('managed_by_tag') or resolve_managed_by_dict(tags_dict)
            except Exception:
                managed_by = 'manual'
                rtags = {'environment': None, 'system': None, 'team': None, 'tier': None}
            env_vars = fn.get('Environment', {}).get('Variables', {})
            memory_size = fn.get('MemorySize', -1)
            fns.append({
                'name': fn_name,
                'arn': fn_arn,
                'runtime': fn.get('Runtime', ''),
                'managed_by': managed_by,
                'env_vars': env_vars,
                'memory_size': memory_size,
                'environment': rtags['environment'],
                'system': rtags['system'],
                'team': rtags['team'],
                'tag_tier': rtags['tier'],
            })
    logger.info(f"Lambda functions: {len(fns)}")
    return fns


def collect_step_functions(sfn_client) -> list:
    sms = []
    paginator = sfn_client.get_paginator('list_state_machines')
    for page in paginator.paginate():
        for sm in page['stateMachines']:
            sm_arn = sm['stateMachineArn']
            try:
                tags_resp = sfn_client.list_tags_for_resource(resourceArn=sm_arn)
                tag_dict = {t['key']: t['value'] for t in tags_resp.get('tags', [])}
                managed_by = resolve_managed_by_dict(tag_dict)
            except Exception:
                managed_by = 'manual'
            try:
                defn = sfn_client.describe_state_machine(stateMachineArn=sm_arn)
                defn_str = defn.get('definition', '')
            except Exception:
                defn_str = ''
            sms.append({
                'name': sm['name'],
                'arn': sm_arn,
                'managed_by': managed_by,
                'definition': defn_str,
            })
    logger.info(f"Step Functions: {len(sms)}")
    return sms


def extract_sfn_lambda_refs(definition_str, lambda_fns):
    """Parse SFN definition JSON and return referenced Lambda function names."""
    if not definition_str:
        return []
    refs = []
    try:
        defn = json.loads(definition_str)
        lambda_arns = set()

        def scan(obj):
            if isinstance(obj, dict):
                resource = obj.get('Resource', '')
                if resource and ('lambda' in resource.lower() or ':function:' in resource):
                    lambda_arns.add(resource)
                for v in obj.values():
                    scan(v)
            elif isinstance(obj, list):
                for item in obj:
                    scan(item)

        scan(defn)
        lambda_name_map = {fn['arn']: fn['name'] for fn in lambda_fns}
        lambda_name_map.update({fn['name']: fn['name'] for fn in lambda_fns})
        for arn in lambda_arns:
            clean_arn = arn.replace(':sync', '').replace(':async', '').rstrip('*').rstrip(':')
            if clean_arn in lambda_name_map:
                refs.append(lambda_name_map[clean_arn])
            else:
                fn_name = clean_arn.split(':')[-1]
                if fn_name in lambda_name_map:
                    refs.append(fn_name)
    except Exception as e:
        logger.warning(f"SFN definition parse failed: {e}")
    return list(set(refs))
