"""OAuth 2.1 authorization server for ChatGPT/Claude/Cursor MCP clients.

The MCP client authenticates to Nabux. During /authorize, the person is sent to
Instagram Business Login (Meta official OAuth). Meta credentials stay on this
server; the MCP access token identifies only the local owner.
"""
from __future__ import annotations

import time
from typing import Optional

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from . import config, security
from .db import Database, get_db

SCOPE = "instagram"
PERSONAL_CLIENT_ID = "personal"


class OwnerAccessToken(AccessToken):
    owner_id: int = 0


class OwnerRefreshToken(RefreshToken):
    owner_id: int = 0
    pair_id: str = ""


class OwnerAuthCode(AuthorizationCode):
    owner_id: int = 0


class Transaction:
    __slots__ = ("id", "client_id", "client_name", "params", "created")

    def __init__(self, id: str, client: OAuthClientInformationFull, params: AuthorizationParams):
        self.id = id
        self.client_id = client.client_id or ""
        self.client_name = client.client_name or self.client_id
        self.params = params
        self.created = time.time()


class InstagramOAuthProvider(OAuthAuthorizationServerProvider[OwnerAuthCode, OwnerRefreshToken, OwnerAccessToken]):
    def __init__(self, db: Optional[Database] = None):
        self._db = db
        self.transactions: dict[str, Transaction] = {}

    @property
    def db(self) -> Database:
        return self._db or get_db()

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        data = self.db.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            client_info.client_id = security.new_token(16)
        self.db.save_client(client_info.client_id, client_info.model_dump(mode="json", exclude_none=True))

    def _sweep(self) -> None:
        cutoff = time.time() - config.AUTH_CODE_TTL
        for key, txn in list(self.transactions.items()):
            if txn.created < cutoff:
                self.transactions.pop(key, None)

    def _params_dict(self, params: AuthorizationParams) -> dict:
        resource = getattr(params, "resource", None)
        return {
            "state": getattr(params, "state", None),
            "scopes": list(getattr(params, "scopes", None) or []),
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": bool(params.redirect_uri_provided_explicitly),
            "resource": str(resource) if resource else None,
        }

    def _txn_from_row(self, row: dict) -> Transaction:
        # Attribute bag, not the SDK model: the stored redirect is already
        # validated when /authorize accepted the client.
        params = AuthorizationParams.model_construct(
            state=row.get("state"),
            scopes=row.get("scopes") or None,
            code_challenge=row.get("code_challenge") or "",
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(row.get("redirect_uri_provided_explicitly")),
            resource=row.get("resource"),
        )
        client = OAuthClientInformationFull.model_construct(
            client_id=row.get("client_id") or "",
            client_name=row.get("client_name") or None,
            redirect_uris=[AnyUrl(row["redirect_uri"])],
        )
        return Transaction(row["id"], client, params)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._sweep()
        if params.scopes and any(scope != SCOPE for scope in params.scopes):
            raise AuthorizeError("invalid_scope", f"Only the '{SCOPE}' scope is supported.")
        txn = Transaction(security.new_token(24), client, params)
        self.transactions[txn.id] = txn
        # Durable copy: the browser that returns from Instagram may hit another
        # process than the one that started /authorize.
        self.db.save_oauth_transaction(
            txn.id,
            txn.client_id,
            txn.client_name,
            self._params_dict(params),
            time.time() + config.AUTH_CODE_TTL,
        )
        return f"{config.PUBLIC_BASE_URL}/connect?txn={txn.id}"

    def get_transaction(self, txn_id: Optional[str]) -> Optional[Transaction]:
        self._sweep()
        found = self.transactions.get(txn_id or "")
        if found:
            return found
        row = self.db.get_oauth_transaction(txn_id or "")
        return self._txn_from_row(row) if row else None

    def complete_transaction(self, txn_id: str, owner_id: int) -> str:
        txn = self.transactions.pop(txn_id, None)
        row = self.db.pop_oauth_transaction(txn_id)
        if txn is None and row is not None:
            txn = self._txn_from_row(row)
        if txn is None:
            raise ValueError("Authorization request expired. Start again from your MCP client.")
        p = txn.params
        code = security.new_token(32)
        self.db.save_auth_code(
            code,
            txn.client_id,
            owner_id,
            {
                "scopes": [SCOPE],
                "code_challenge": p.code_challenge,
                "redirect_uri": str(p.redirect_uri),
                "redirect_uri_provided_explicitly": p.redirect_uri_provided_explicitly,
                "resource": p.resource,
            },
            time.time() + config.AUTH_CODE_TTL,
        )
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    def deny_transaction(self, txn_id: str) -> Optional[str]:
        txn = self.transactions.pop(txn_id, None)
        row = self.db.pop_oauth_transaction(txn_id)
        if txn is None and row is not None:
            txn = self._txn_from_row(row)
        if not txn:
            return None
        return construct_redirect_uri(str(txn.params.redirect_uri), error="access_denied", state=txn.params.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> Optional[OwnerAuthCode]:
        d = self.db.peek_auth_code(authorization_code)
        if d is None or d["client_id"] != client.client_id:
            return None
        return OwnerAuthCode(
            code=d["code"], scopes=d["scopes"], expires_at=d["expires_at"], client_id=d["client_id"],
            code_challenge=d["code_challenge"], redirect_uri=AnyUrl(d["redirect_uri"]),
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
            resource=d.get("resource"), owner_id=d["owner_id"],
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: OwnerAuthCode) -> OAuthToken:
        d = self.db.pop_auth_code(authorization_code.code)
        if d is None or d["expires_at"] < time.time():
            raise TokenError("invalid_grant", "authorization code expired or already used")
        return self._issue(client.client_id or "", d["owner_id"], d["scopes"], resource=d.get("resource"))

    def _issue(self, client_id: str, owner_id: int, scopes: list[str],
               resource: Optional[str] = None, label: Optional[str] = None) -> OAuthToken:
        pair = security.new_token(12)
        access = security.new_token(32)
        refresh = security.new_token(32)
        now = int(time.time())
        self.db.save_token(access, kind="access", client_id=client_id, owner_id=owner_id, scopes=scopes,
                           pair_id=pair, expires_at=now + config.ACCESS_TOKEN_TTL, label=label)
        self.db.save_token(refresh, kind="refresh", client_id=client_id, owner_id=owner_id, scopes=scopes,
                           pair_id=pair, expires_at=now + config.REFRESH_TOKEN_TTL, label=label)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=config.ACCESS_TOKEN_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    def issue_personal_token(self, owner_id: int, label: str = "personal",
                             default_account_key: Optional[str] = None) -> str:
        token = security.new_token(32)
        self.db.save_token(token, kind="access", client_id=PERSONAL_CLIENT_ID, owner_id=owner_id,
                           scopes=[SCOPE], pair_id=security.new_token(12), expires_at=None, label=label,
                           default_account_key=default_account_key)
        return token

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> Optional[OwnerRefreshToken]:
        d = self.db.get_token(refresh_token, kind="refresh")
        if d is None or d["client_id"] != client.client_id:
            return None
        return OwnerRefreshToken(token=refresh_token, client_id=d["client_id"], scopes=d["scopes"],
                                 expires_at=d["expires_at"], owner_id=d["owner_id"], pair_id=d["pair_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: OwnerRefreshToken, scopes: list[str]) -> OAuthToken:
        if not self.db.get_owner(refresh_token.owner_id):
            raise TokenError("invalid_grant", "owner disconnected")
        self.db.revoke_pair(refresh_token.pair_id)
        return self._issue(client.client_id or "", refresh_token.owner_id, scopes or [SCOPE])

    async def load_access_token(self, token: str) -> Optional[OwnerAccessToken]:
        d = self.db.get_token(token, kind="access")
        if d is None:
            return None
        return OwnerAccessToken(token=token, client_id=d["client_id"], scopes=d["scopes"],
                                expires_at=d["expires_at"], owner_id=d["owner_id"])

    async def revoke_token(self, token) -> None:
        d = self.db.get_token(token.token)
        if d:
            self.db.revoke_pair(d["pair_id"])


_provider: Optional[InstagramOAuthProvider] = None


def get_provider() -> InstagramOAuthProvider:
    global _provider
    if _provider is None:
        _provider = InstagramOAuthProvider()
    return _provider
