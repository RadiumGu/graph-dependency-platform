"""
collectors/ec2.py - EC2 instances, subnets, VPCs, security groups collectors.
"""

import logging
from neptune_client import resolve_managed_by, resolve_managed_by_dict, resolve_resource_tags
from config import SKIP_SG_VPCS

logger = logging.getLogger()


def collect_ec2_instances(ec2_client) -> list:
    nodes = []
    paginator = ec2_client.get_paginator('describe_instances')
    # Collect all states (running/stopped/stopping/shutting-down) for graph accuracy.
    # Skip 'terminated' to avoid stale entries (ASG replacements).
    for page in paginator.paginate(
        Filters=[{'Name': 'instance-state-name',
                  'Values': ['running', 'stopped', 'stopping', 'shutting-down', 'pending']}]
    ):
        for reservation in page['Reservations']:
            for inst in reservation['Instances']:
                instance_id = inst['InstanceId']
                tags = inst.get('Tags', [])
                name = next((t['Value'] for t in tags if t['Key'] == 'Name'), instance_id)
                is_eks_node = any(t['Key'].startswith('eks:') for t in tags)
                rtags = resolve_resource_tags(tags)
                managed_by = rtags.get('managed_by_tag') or resolve_managed_by(tags)
                nodes.append({
                    'id': instance_id,
                    'name': name,
                    'state': inst.get('State', {}).get('Name', 'unknown'),
                    'instance_type': inst.get('InstanceType', ''),
                    'az': inst.get('Placement', {}).get('AvailabilityZone', ''),
                    'private_ip': inst.get('PrivateIpAddress', ''),
                    'private_dns': inst.get('PrivateDnsName', ''),
                    'subnet_id': inst.get('SubnetId', ''),
                    'vpc_id': inst.get('VpcId', ''),
                    'managed_by': managed_by,
                    'is_eks_node': is_eks_node,
                    'environment': rtags['environment'],
                    'system': rtags['system'],
                    'team': rtags['team'],
                    'tag_tier': rtags['tier'],
                })
    logger.info(f"EC2 instances: {len(nodes)}")
    return nodes


def collect_subnets(ec2_client) -> list:
    subnets = []
    paginator = ec2_client.get_paginator('describe_subnets')
    for page in paginator.paginate():
        for sn in page['Subnets']:
            tags = sn.get('Tags', [])
            name = next((t['Value'] for t in tags if t['Key'] == 'Name'), sn['SubnetId'])
            subnets.append({
                'subnet_id': sn['SubnetId'],
                'name': name,
                'cidr': sn.get('CidrBlock', ''),
                'az': sn.get('AvailabilityZone', ''),
                'vpc_id': sn.get('VpcId', ''),
            })
    logger.info(f"Subnets: {len(subnets)}")
    return subnets


def collect_vpcs(ec2_client) -> list:
    vpcs = []
    try:
        resp = ec2_client.describe_vpcs()
        for v in resp.get('Vpcs', []):
            tags = {t['Key']: t['Value'] for t in v.get('Tags', [])}
            vpcs.append({
                'vpc_id': v['VpcId'],
                'name':   tags.get('Name', v['VpcId']),
                'cidr':   v.get('CidrBlock', ''),
            })
    except Exception as e:
        logger.warning(f"collect_vpcs: {e}")
    logger.info(f"VPCs: {len(vpcs)}")
    return vpcs


def collect_security_groups(ec2_client) -> list:
    sgs = []
    try:
        paginator = ec2_client.get_paginator('describe_security_groups')
        for page in paginator.paginate():
            for sg in page.get('SecurityGroups', []):
                if sg.get('VpcId', '') in SKIP_SG_VPCS:
                    continue
                sgs.append({
                    'sg_id':       sg['GroupId'],
                    'name':        sg['GroupName'],
                    'description': sg.get('Description', ''),
                    'vpc_id':      sg.get('VpcId', ''),
                })
    except Exception as e:
        logger.warning(f"collect_security_groups: {e}")
    logger.info(f"SecurityGroups: {len(sgs)}")
    return sgs
