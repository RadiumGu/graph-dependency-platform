"""
registry — Service Type Registry package

Provides centralized AWS service type classification consumed by all modules.
"""

from registry.registry_loader import ServiceTypeInfo, ServiceTypeRegistry

__all__ = ["ServiceTypeInfo", "ServiceTypeRegistry"]
