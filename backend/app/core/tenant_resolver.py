"""
Minimal tenant resolver for authentication.
"""
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TenantResolver:
    """Minimal tenant resolver that extracts tenant_id from JWT claims."""

    @staticmethod
    def resolve_tenant_from_token(token_payload: dict) -> Optional[str]:
        """
        Extract tenant_id from JWT token payload.

        Args:
            token_payload: Decoded JWT payload

        Returns:
            Tenant ID if found, None otherwise
        """
        # Authorization uses server-controlled claims, never user_metadata.
        app_metadata = token_payload.get('app_metadata')
        if isinstance(app_metadata, dict) and 'tenant_id' in app_metadata:
            tenant_id = app_metadata['tenant_id']
        else:
            tenant_id = token_payload.get('tenant_id')

        if isinstance(tenant_id, str) and tenant_id.strip() and tenant_id == tenant_id.strip():
            return tenant_id

        logger.warning("No tenant_id found in token payload")
        return None

    @staticmethod
    def resolve_tenant_from_user(user_data: dict) -> Optional[str]:
        """
        Extract tenant_id from user data.

        Args:
            user_data: User data dictionary

        Returns:
            Tenant ID if found, None otherwise
        """
        return TenantResolver.resolve_tenant_from_token(user_data)

    @staticmethod
    async def resolve_tenant_id(
        user_id: str,
        user_email: str,
        token: Optional[str] = None,
        verified_payload: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Resolve tenant ID for a user.
        
        Args:
            user_id: User ID
            user_email: User email
            verified_payload: Claims or app metadata from verified authentication
            
        Returns:
            Tenant ID
        """
        if verified_payload is None:
            logger.warning("Cannot resolve a tenant without verified authentication claims")
            return None
        return TenantResolver.resolve_tenant_from_token(verified_payload)

    @staticmethod
    async def update_user_tenant_metadata(user_id: str, tenant_id: str) -> None:
        """
        Update user metadata with tenant_id.
        
        Args:
            user_id: User ID
            tenant_id: Tenant ID
        """
        # No-op in this resolver implementation.
        pass
