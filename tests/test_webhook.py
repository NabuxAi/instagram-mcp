import hashlib
import hmac
import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret-that-is-long-enough-for-fernet-123456")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("IG_APP_ID", "test-app")
os.environ.setdefault("IG_APP_SECRET", "test-secret")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "internal-test-token")

from app.web import _normalize_webhook_events, _verify_webhook_signature


def _dm_payload(ig_user_id="17841400000000001", sender_id="990", text="hi", mid="mid.abc", **message_extra):
    return {
        "object": "instagram",
        "entry": [{
            "id": ig_user_id,
            "time": 1234567890,
            "messaging": [{
                "sender": {"id": sender_id},
                "recipient": {"id": ig_user_id},
                "timestamp": 1234567890,
                "message": {"mid": mid, "text": text, **message_extra},
            }],
        }],
    }


def _comment_payload(ig_user_id="17841400000000001", sender_id="990", username="a_customer", text="nice!", comment_id="c1"):
    return {
        "object": "instagram",
        "entry": [{
            "id": ig_user_id,
            "time": 1234567890,
            "changes": [{
                "field": "comments",
                "value": {
                    "id": comment_id,
                    "text": text,
                    "from": {"id": sender_id, "username": username},
                    "media": {"id": "media_1"},
                },
            }],
        }],
    }


def test_a_direct_message_normalizes_to_one_event():
    events = _normalize_webhook_events(_dm_payload())

    assert events == [{
        "ig_user_id": "17841400000000001",
        "type": "message",
        "sender_id": "990",
        "message_id": "mid.abc",
        "text": "hi",
    }]


def test_the_accounts_own_echoed_reply_is_not_relayed_as_an_inbound_message():
    events = _normalize_webhook_events(_dm_payload(is_echo=True))

    assert events == []


def test_a_deleted_message_notification_is_not_relayed():
    events = _normalize_webhook_events(_dm_payload(is_deleted=True))

    assert events == []


def test_a_comment_normalizes_with_the_commenters_username():
    events = _normalize_webhook_events(_comment_payload())

    assert events == [{
        "ig_user_id": "17841400000000001",
        "type": "comment",
        "sender_id": "990",
        "sender_username": "a_customer",
        "message_id": "c1",
        "text": "nice!",
    }]


def test_a_change_that_is_not_a_comment_is_ignored():
    payload = {
        "object": "instagram",
        "entry": [{"id": "1", "changes": [{"field": "story_insights", "value": {}}]}],
    }

    assert _normalize_webhook_events(payload) == []


def test_multiple_entries_and_events_in_one_delivery_all_normalize():
    payload = _dm_payload(ig_user_id="1", sender_id="a", text="one", mid="m1")
    payload["entry"].append(_dm_payload(ig_user_id="2", sender_id="b", text="two", mid="m2")["entry"][0])

    events = _normalize_webhook_events(payload)

    assert [e["ig_user_id"] for e in events] == ["1", "2"]
    assert [e["sender_id"] for e in events] == ["a", "b"]


def test_signature_verification_matches_metas_hmac_sha256_scheme():
    body = b'{"object":"instagram"}'
    secret = "test-secret"
    good_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert _verify_webhook_signature(body, good_sig) is True
    assert _verify_webhook_signature(body, "sha256=deadbeef") is False
    assert _verify_webhook_signature(body, "not-even-the-right-scheme") is False


def test_a_page_delivery_signed_with_the_facebook_app_secret_is_also_genuine(monkeypatch):
    # One Meta app, two products, one callback: Instagram Login deliveries are
    # signed with IG_APP_SECRET, Page/Messenger deliveries with FB_APP_SECRET.
    from app import config

    monkeypatch.setattr(config, "IG_APP_SECRET", "ig-secret")
    monkeypatch.setattr(config, "FB_APP_SECRET", "fb-secret")
    body = b'{"object":"page"}'

    fb_sig = "sha256=" + hmac.new(b"fb-secret", body, hashlib.sha256).hexdigest()
    ig_sig = "sha256=" + hmac.new(b"ig-secret", body, hashlib.sha256).hexdigest()
    other = "sha256=" + hmac.new(b"someone-else", body, hashlib.sha256).hexdigest()

    assert _verify_webhook_signature(body, fb_sig) is True
    assert _verify_webhook_signature(body, ig_sig) is True
    assert _verify_webhook_signature(body, other) is False


def test_an_unset_facebook_secret_is_skipped_not_treated_as_empty_key(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "IG_APP_SECRET", "ig-secret")
    monkeypatch.setattr(config, "FB_APP_SECRET", "")
    body = b'{}'
    empty_key_sig = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()

    assert _verify_webhook_signature(body, empty_key_sig) is False


# --- messages that carry no words -------------------------------------------
# Requiring `text` dropped every photo, voice note, sticker, GIF, shared reel
# and media-only story reply on the floor, silently — a large share of real
# Instagram traffic. They are filed under what they are instead.


def _attachment_payload(kind="image", ig_user_id="17841400000000001", sender_id="990"):
    return {
        "object": "instagram",
        "entry": [{
            "id": ig_user_id,
            "messaging": [{
                "sender": {"id": sender_id},
                "recipient": {"id": ig_user_id},
                "message": {"mid": "mid.att", "attachments": [{"type": kind, "payload": {"url": "https://x/y"}}]},
            }],
        }],
    }


def test_a_photo_with_no_caption_is_still_an_event():
    events = _normalize_webhook_events(_attachment_payload("image"))

    assert len(events) == 1
    assert events[0]["type"] == "message"
    assert events[0]["text"] == "[photo]"
    assert events[0]["message_id"] == "mid.att"


def test_every_attachment_kind_survives_normalization():
    for kind, label in [
        ("video", "[video]"),
        ("audio", "[voice message]"),
        ("share", "[shared post]"),
        ("story_mention", "[story mention]"),
        ("ig_reel", "[reel]"),
    ]:
        events = _normalize_webhook_events(_attachment_payload(kind))
        assert [e["text"] for e in events] == [label], kind


def test_an_attachment_kind_meta_invents_later_is_labelled_not_dropped():
    events = _normalize_webhook_events(_attachment_payload("hologram"))

    assert [e["text"] for e in events] == ["[hologram]"]


def test_a_message_with_neither_text_nor_attachments_is_still_ignored():
    payload = _dm_payload(text=None)
    payload["entry"][0]["messaging"][0]["message"].pop("text", None)

    assert _normalize_webhook_events(payload) == []


def test_an_empty_text_does_not_become_an_event():
    assert _normalize_webhook_events(_dm_payload(text="   ")) == []


# --- reactions ---------------------------------------------------------------


def _reaction_payload(action="react", emoji="\u2764\ufe0f", ig_user_id="17841400000000001"):
    return {
        "object": "instagram",
        "entry": [{
            "id": ig_user_id,
            "messaging": [{
                "sender": {"id": "990"},
                "recipient": {"id": ig_user_id},
                "reaction": {"mid": "mid.reacted", "action": action, "reaction": "love", "emoji": emoji},
            }],
        }],
    }


def test_a_reaction_is_its_own_event():
    events = _normalize_webhook_events(_reaction_payload())

    assert len(events) == 1
    assert events[0]["type"] == "reaction"
    assert events[0]["message_id"] == "mid.reacted"
    assert "\u2764\ufe0f" in events[0]["text"]


def test_removing_a_reaction_is_not_an_event():
    assert _normalize_webhook_events(_reaction_payload(action="unreact")) == []


# --- live comments -----------------------------------------------------------


def test_a_comment_on_a_live_video_normalizes_like_any_other():
    payload = _comment_payload()
    payload["entry"][0]["changes"][0]["field"] = "live_comments"

    events = _normalize_webhook_events(payload)

    assert len(events) == 1
    assert events[0]["type"] == "comment"
    assert events[0]["text"] == "nice!"
