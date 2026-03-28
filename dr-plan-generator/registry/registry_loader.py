"""
registry/registry_loader.py — Service Type Registry loader

Loads service type definitions from service_types.yaml (defaults) and
optionally merges custom_types.yaml (customer overrides, higher priority).

Unknown types are handled conservatively: log WARNING + return defaults
(layer=L2, fault_domain=zonal, spof_candidate=True).
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_YAML = os.path.join(os.path.dirname(__file__), "service_types.yaml")

# Conservative defaults for unknown types
_UNKNOWN_DEFAULTS = {
    "layer": "L2",
    "fault_domain": "zonal",
    "spof_candidate": True,
    "has_step_builder": False,
    "switchover_type": "manual_switchover",
    "description": "未知资源类型（保守降级）",
    "validation_template": 'echo "TODO: Add validation for {type} {name}" && exit 1',
}


@dataclass
class ServiceTypeInfo:
    """Metadata for a single AWS service type.

    Attributes:
        type_name: The resource type name (e.g. ``RDSCluster``).
        layer: Switchover layer (``L0``–``L3``).
        fault_domain: Fault isolation scope (``zonal`` / ``regional`` / ``global``).
        spof_candidate: Whether this type is an AZ-bound SPOF candidate.
        has_step_builder: Whether StepBuilder has a dedicated builder method.
        switchover_type: How to switch this resource over (e.g. ``promote_replica``).
        description: Human-readable Chinese description.
        validation_template: Executable AWS CLI / kubectl command template to verify the
            resource after switchover. Supports ``{name}``, ``{target}``, and ``{type}``
            placeholders.
        is_unknown: True when this info was synthesized from defaults (type not in YAML).
    """

    type_name: str
    layer: str
    fault_domain: str
    spof_candidate: bool
    has_step_builder: bool
    switchover_type: str
    description: str
    validation_template: str = ""
    is_unknown: bool = False


class ServiceTypeRegistry:
    """Centralized registry of AWS service type classifications.

    Loaded once at startup. Optionally merges a custom YAML for
    customer-specific overrides (custom YAML takes precedence).

    Args:
        custom_path: Optional path to a custom_types.yaml file.
    """

    def __init__(self, custom_path: Optional[str] = None) -> None:
        """Initialize the registry, loading default and optional custom YAML.

        Args:
            custom_path: Path to custom_types.yaml. If None, only the bundled
                service_types.yaml is loaded.
        """
        self._types: Dict[str, ServiceTypeInfo] = {}
        self._load(_DEFAULT_YAML)
        if custom_path:
            self._load(custom_path, override=True)

    # ------------------------------------------------------------------
    # Public lookup API
    # ------------------------------------------------------------------

    def get_type(self, type_name: str) -> ServiceTypeInfo:
        """Return ServiceTypeInfo for the given type name.

        For unknown types, logs a WARNING and returns conservative defaults.

        Args:
            type_name: Resource type name (e.g. ``RDSCluster``).

        Returns:
            ServiceTypeInfo (possibly with ``is_unknown=True`` for unknowns).
        """
        if type_name in self._types:
            return self._types[type_name]
        logger.warning(
            "Unknown resource type %r — using conservative defaults (layer=L2, "
            "fault_domain=zonal, spof_candidate=True). "
            "Consider adding it to registry/custom_types.yaml.",
            type_name,
        )
        return ServiceTypeInfo(
            type_name=type_name,
            layer=_UNKNOWN_DEFAULTS["layer"],
            fault_domain=_UNKNOWN_DEFAULTS["fault_domain"],
            spof_candidate=_UNKNOWN_DEFAULTS["spof_candidate"],
            has_step_builder=_UNKNOWN_DEFAULTS["has_step_builder"],
            switchover_type=_UNKNOWN_DEFAULTS["switchover_type"],
            description=_UNKNOWN_DEFAULTS["description"],
            validation_template=_UNKNOWN_DEFAULTS["validation_template"],
            is_unknown=True,
        )

    def get_layer(self, type_name: str) -> str:
        """Return the switchover layer for a resource type.

        Args:
            type_name: Resource type name.

        Returns:
            Layer string (``L0``–``L3``); ``L2`` for unknown types.
        """
        return self.get_type(type_name).layer

    def is_spof_candidate(self, type_name: str) -> bool:
        """Return whether a resource type is an AZ-bound SPOF candidate.

        Args:
            type_name: Resource type name.

        Returns:
            True if the type is a SPOF candidate; True for unknown types
            (conservative assumption).
        """
        return self.get_type(type_name).spof_candidate

    def get_fault_domain(self, type_name: str) -> str:
        """Return the fault domain for a resource type.

        Args:
            type_name: Resource type name.

        Returns:
            Fault domain (``zonal`` / ``regional`` / ``global``);
            ``zonal`` for unknown types.
        """
        return self.get_type(type_name).fault_domain

    def list_types_by_layer(self, layer: str) -> List[str]:
        """Return all registered type names in a given layer.

        Args:
            layer: Layer string (``L0``–``L3``).

        Returns:
            Sorted list of type names in that layer.
        """
        return sorted(
            name for name, info in self._types.items() if info.layer == layer
        )

    def list_spof_candidates(self) -> List[str]:
        """Return all registered type names that are SPOF candidates.

        Returns:
            Sorted list of type names with ``spof_candidate=True``.
        """
        return sorted(
            name for name, info in self._types.items() if info.spof_candidate
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load(self, path: str, override: bool = False) -> None:
        """Load a YAML file and merge its types into the registry.

        Args:
            path: Path to the YAML file.
            override: If True, custom entries overwrite existing ones.

        Raises:
            FileNotFoundError: If the YAML file does not exist.
            ValueError: If the YAML structure is invalid.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Registry YAML not found: {path}")

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)

        if not isinstance(data, dict) or "service_types" not in data:
            raise ValueError(
                f"Invalid registry YAML {path!r}: "
                "expected top-level 'service_types' key."
            )

        loaded = 0
        for type_name, raw in data["service_types"].items():
            if not override and type_name in self._types:
                continue
            self._types[type_name] = ServiceTypeInfo(
                type_name=type_name,
                layer=raw.get("layer", _UNKNOWN_DEFAULTS["layer"]),
                fault_domain=raw.get("fault_domain", _UNKNOWN_DEFAULTS["fault_domain"]),
                spof_candidate=bool(raw.get("spof_candidate", _UNKNOWN_DEFAULTS["spof_candidate"])),
                has_step_builder=bool(raw.get("has_step_builder", False)),
                switchover_type=raw.get("switchover_type", _UNKNOWN_DEFAULTS["switchover_type"]),
                description=raw.get("description", ""),
                validation_template=raw.get("validation_template", _UNKNOWN_DEFAULTS["validation_template"]),
            )
            loaded += 1

        source = "custom" if override else "default"
        logger.debug("Loaded %d service types from %s registry: %s", loaded, source, path)


# ---------------------------------------------------------------------------
# Module-level singleton — loaded once at import time
# ---------------------------------------------------------------------------

_registry: Optional[ServiceTypeRegistry] = None


def get_registry(custom_path: Optional[str] = None) -> ServiceTypeRegistry:
    """Return the module-level singleton registry.

    On first call (or when ``custom_path`` is provided), instantiates the
    registry. Subsequent calls with no ``custom_path`` return the cached instance.

    Args:
        custom_path: Optional path to a custom_types.yaml to merge.

    Returns:
        Shared ServiceTypeRegistry instance.
    """
    global _registry
    if _registry is None or custom_path is not None:
        _registry = ServiceTypeRegistry(custom_path=custom_path)
    return _registry
