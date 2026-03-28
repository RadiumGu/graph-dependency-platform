"""
output/json_renderer.py — Serialize DR plans to JSON

Produces machine-consumable JSON suitable for automation systems,
CI/CD pipelines, and programmatic plan loading.
"""

import dataclasses
import json
import logging
from typing import Any

from models import DRPlan

logger = logging.getLogger(__name__)


class JSONRenderer:
    """Serialize DRPlan objects to JSON strings."""

    def render(self, plan: DRPlan) -> str:
        """Serialize a DRPlan to a formatted JSON string.

        Args:
            plan: The DRPlan to serialize.

        Returns:
            JSON string with 2-space indentation.
        """
        return json.dumps(
            dataclasses.asdict(plan),
            indent=2,
            ensure_ascii=False,
            default=self._json_default,
        )

    @staticmethod
    def _json_default(obj: Any) -> Any:
        """Fallback JSON serializer for non-standard types.

        Args:
            obj: Object to serialize.

        Returns:
            JSON-serializable representation.

        Raises:
            TypeError: If the object cannot be serialized.
        """
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
