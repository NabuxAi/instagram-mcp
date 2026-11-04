import os
import tempfile

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from app.db import Database


def test_multi_account_tokens_are_encrypted_and_owner_scoped():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"{tmp}/test.db")
        owner_a = database.create_owner()
        owner_b = database.create_owner()

        a1 = database.upsert_account(
            owner_id=owner_a,
            ig_user_id="17840000000000001",
            username="brand_one",
            account_type="BUSINESS",
            access_token="META_TOKEN_A",
            expires_at=None,
        )
        a2 = database.upsert_account(
            owner_id=owner_a,
            ig_user_id="17840000000000002",
            username="brand_two",
            account_type="CREATOR",
            access_token="META_TOKEN_B",
            expires_at=None,
        )

        assert database.account_token(a1["id"]) == "META_TOKEN_A"
        assert database.account_token(a2["id"]) == "META_TOKEN_B"
        assert a1["token_enc"] != "META_TOKEN_A"
        assert a2["token_enc"] != "META_TOKEN_B"
        assert len(database.accounts_for_owner(owner_a)) == 2
        assert database.accounts_for_owner(owner_b) == []
        assert a1["account_key"].startswith("ig_")
        assert a1["account_key"] != a2["account_key"]


def test_existing_instagram_account_cannot_be_moved_to_another_owner():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"{tmp}/test.db")
        owner_a = database.create_owner()
        owner_b = database.create_owner()
        database.upsert_account(
            owner_id=owner_a,
            ig_user_id="17840000000000003",
            username="owner_a",
            account_type="BUSINESS",
            access_token="META_TOKEN_A",
            expires_at=None,
        )
        try:
            database.upsert_account(
                owner_id=owner_b,
                ig_user_id="17840000000000003",
                username="owner_a",
                account_type="BUSINESS",
                access_token="META_TOKEN_OTHER",
                expires_at=None,
            )
        except ValueError as exc:
            assert "already connected" in str(exc)
        else:
            raise AssertionError("account ownership must not be transferable by reconnecting")


def test_default_account_is_stable():
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(f"{tmp}/test.db")
        owner = database.create_owner()
        first = database.upsert_account(
            owner_id=owner,
            ig_user_id="17840000000000004",
            username="first",
            account_type="BUSINESS",
            access_token="A",
            expires_at=None,
        )
        second = database.upsert_account(
            owner_id=owner,
            ig_user_id="17840000000000005",
            username="second",
            account_type="BUSINESS",
            access_token="B",
            expires_at=None,
        )
        assert database.default_account(owner)["id"] == first["id"]
        assert database.set_default_account(owner, second["id"])
        assert database.default_account(owner)["id"] == second["id"]
