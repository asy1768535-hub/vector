from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.user import User

_API_KEY_AUDIT_IDENTITY_ATTRIBUTE = "_stable_predicate_api_key_audit_identity"


class StablePredicateActorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CredentialApiKeyAuditIdentity:
    organization_id: uuid.UUID
    api_key_id: uuid.UUID


def bind_credential_api_key_audit_identity(
    user: User,
    organization_id: uuid.UUID,
    api_key_id: uuid.UUID,
) -> None:
    if not isinstance(organization_id, uuid.UUID) or not isinstance(api_key_id, uuid.UUID):
        raise StablePredicateActorError("API key audit identity requires UUID values")
    setattr(
        user,
        _API_KEY_AUDIT_IDENTITY_ATTRIBUTE,
        CredentialApiKeyAuditIdentity(
            organization_id=organization_id,
            api_key_id=api_key_id,
        ),
    )


def credential_api_key_audit_identity(
    user: User,
) -> CredentialApiKeyAuditIdentity | None:
    value = getattr(user, _API_KEY_AUDIT_IDENTITY_ATTRIBUTE, None)
    return value if isinstance(value, CredentialApiKeyAuditIdentity) else None


__all__ = [
    "CredentialApiKeyAuditIdentity",
    "StablePredicateActorError",
    "bind_credential_api_key_audit_identity",
    "credential_api_key_audit_identity",
]
