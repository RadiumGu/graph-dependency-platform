"""
graph/neptune_client.py — Neptune openCypher client with SigV4 auth

Mirrors the pattern in rca/neptune/neptune_client.py but lives inside
the dr-plan-generator package so it can be used independently.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from config import NEPTUNE_ENDPOINT, NEPTUNE_PORT, REGION

logger = logging.getLogger(__name__)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """Return a cached requests Session."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def query(cypher: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute an openCypher query against Neptune and return the raw response dict.

    Args:
        cypher: The openCypher query string.
        params: Optional query parameters dict.

    Returns:
        Parsed JSON response from Neptune.

    Raises:
        requests.HTTPError: If Neptune returns a non-2xx status.
        ValueError: If NEPTUNE_ENDPOINT is not configured.
    """
    if not NEPTUNE_ENDPOINT:
        raise ValueError(
            "NEPTUNE_ENDPOINT environment variable is not set. "
            "Export it before running dr-plan-generator."
        )

    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/openCypher"
    body_dict: Dict[str, Any] = {"query": cypher}
    if params:
        body_dict["parameters"] = json.dumps(params)
    body = json.dumps(body_dict).encode()

    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)

    resp = _get_session().post(
        url,
        data=body,
        headers=dict(aws_request.headers),
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def results(cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Execute an openCypher query and return the results list.

    Args:
        cypher: The openCypher query string.
        params: Optional query parameters dict.

    Returns:
        List of result row dicts from Neptune.
    """
    data = query(cypher, params)
    return data.get("results", [])


class NeptuneClient:
    """Thin wrapper around module-level query functions for dependency injection."""

    def query(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute openCypher query; delegates to module-level query()."""
        return query(cypher, params)

    def results(self, cypher: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Execute openCypher query and return results list."""
        return results(cypher, params)
