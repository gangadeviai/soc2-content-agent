"""Identity, roles and tenant scoping (CC6.1, CC6.2, CC6.3).

Tokens are issued by the corporate IdP (OIDC). The agent never creates accounts.
Claims used:
  sub        - employee ID (recorded as actor in every audit record)
  tenant_id  - the client the user is working for; drives RAG metadata filters
  roles      - subset of {requester, reviewer, admin}

Segregation of duties (PI1.4): a reviewer may not approve a draft they requested.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError as JWTError

from .config import Settings, get_settings

ROLE_REQUESTER = "requester"
ROLE_REVIEWER = "reviewer"
ROLE_ADMIN = "admin"

_bearer = HTTPBearer(auto_error=True)


@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str
    roles: frozenset[str]

    def has(self, role: str) -> bool:
        return role in self.roles or ROLE_ADMIN in self.roles


def decode_token(token: str, settings: Settings) -> Principal:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
        )
    except JWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
    try:
        return Principal(
            sub=str(claims["sub"]),
            tenant_id=str(claims["tenant_id"]),
            roles=frozenset(claims.get("roles", [])),
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"token missing claim {exc}") from exc


def current_principal(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    return decode_token(creds.credentials, settings)


def require_role(role: str):
    def _dep(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(role):
            # Denials are audited too - auditors ask for evidence that access control *works*.
            sink = getattr(request.app.state, "audit", None)
            if sink:
                from .audit import AuditRecord

                sink.write(
                    AuditRecord(
                        "ACCESS_DENIED",
                        principal.sub,
                        principal.tenant_id,
                        None,
                        {"required_role": role, "path": request.url.path},
                    )
                )
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role {role!r} required")
        return principal

    return _dep


def mint_dev_token(sub: str, tenant_id: str, roles: list[str], settings: Settings) -> str:
    """Local development only. Production tokens come from the IdP."""
    if settings.env == "prod":
        raise RuntimeError("dev token minting is disabled in prod")
    return jwt.encode(
        {"sub": sub, "tenant_id": tenant_id, "roles": roles, "aud": settings.jwt_audience, "iss": settings.jwt_issuer},
        settings.jwt_secret,
        algorithm="HS256",
    )
