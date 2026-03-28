"""
collectors/alb.py - ALB load balancers, listener rules, target groups collectors.
"""

import logging
from neptune_client import resolve_managed_by_dict, resolve_resource_tags
from config import SKIP_TG_PREFIXES

logger = logging.getLogger()


def collect_load_balancers(elb_client) -> list:
    lbs = []
    paginator = elb_client.get_paginator('describe_load_balancers')
    for page in paginator.paginate():
        for lb in page['LoadBalancers']:
            try:
                tags_resp = elb_client.describe_tags(ResourceArns=[lb['LoadBalancerArn']])
                tag_list = tags_resp['TagDescriptions'][0]['Tags'] if tags_resp['TagDescriptions'] else []
                tag_dict = {t['Key']: t['Value'] for t in tag_list}
                rtags = resolve_resource_tags(tag_list)
                managed_by = rtags.get('managed_by_tag') or resolve_managed_by_dict(tag_dict)
            except Exception:
                managed_by = 'manual'
                rtags = {'environment': None, 'system': None, 'team': None, 'tier': None}
            azs = [az_info['ZoneName'] for az_info in lb.get('AvailabilityZones', [])]
            lbs.append({
                'arn': lb['LoadBalancerArn'],
                'name': lb['LoadBalancerName'],
                'dns': lb.get('DNSName', ''),
                'scheme': lb.get('Scheme', ''),
                'type': lb.get('Type', ''),
                'azs': azs,
                'managed_by': managed_by,
                'environment': rtags['environment'],
                'system': rtags['system'],
                'team': rtags['team'],
                'tag_tier': rtags['tier'],
            })
    logger.info(f"Load Balancers: {len(lbs)}")
    return lbs


def collect_listener_rules(elb_client) -> list:
    rules = []
    try:
        lbs = elb_client.describe_load_balancers().get('LoadBalancers', [])
        for lb in lbs:
            lb_arn = lb['LoadBalancerArn']
            listeners = elb_client.describe_listeners(LoadBalancerArn=lb_arn).get('Listeners', [])
            for listener in listeners:
                listener_arn = listener['ListenerArn']
                try:
                    rule_pages = elb_client.get_paginator('describe_rules').paginate(ListenerArn=listener_arn)
                    for page in rule_pages:
                        for rule in page['Rules']:
                            rule_arn = rule['RuleArn']
                            tg_arns = [
                                a.get('TargetGroupArn')
                                for a in rule.get('Actions', [])
                                if a.get('Type') == 'forward' and a.get('TargetGroupArn')
                            ]
                            rules.append({
                                'lb_arn':       lb_arn,
                                'listener_arn': listener_arn,
                                'rule_arn':     rule_arn,
                                'priority':     rule.get('Priority', '999'),
                                'is_default':   rule.get('IsDefault', False),
                                'tg_arns':      tg_arns,
                            })
                except Exception as e:
                    logger.debug(f"describe_rules {listener_arn}: {e}")
    except Exception as e:
        logger.warning(f"collect_listener_rules failed: {e}")
    logger.info(f"Listener rules: {len(rules)}")
    return rules


def collect_alb_target_groups(elb_client) -> list:
    tgs = []
    paginator = elb_client.get_paginator('describe_target_groups')
    for page in paginator.paginate():
        for tg in page['TargetGroups']:
            try:
                if any(tg['TargetGroupName'].startswith(pfx) for pfx in SKIP_TG_PREFIXES):
                    continue
                health = elb_client.describe_target_health(TargetGroupArn=tg['TargetGroupArn'])
                healthy_targets = [
                    desc['Target']['Id']
                    for desc in health.get('TargetHealthDescriptions', [])
                    if desc.get('TargetHealth', {}).get('State') == 'healthy'
                ]
            except Exception:
                healthy_targets = []
            tgs.append({
                'arn': tg['TargetGroupArn'],
                'name': tg['TargetGroupName'],
                'port': tg.get('Port', 0),
                'protocol': tg.get('Protocol', ''),
                'lb_arns': tg.get('LoadBalancerArns', []),
                'healthy_targets': healthy_targets,
            })
    logger.info(f"Target Groups: {len(tgs)}")
    return tgs
