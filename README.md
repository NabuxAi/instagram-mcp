> **Community source release (MIT).** Self-host using your own credentials. No NabuxAI production accounts or hosted-service access are included.

# Instagram MCP by Nabux

**Multi-user Instagram MCP Gateway. Official Meta login and sending stay on their existing path; an optional private-session account type can read Direct messages only.**

> Official API, Nabux gateway: Instagram operations use Meta's supported Instagram API / Business Login. The MCP server itself is built and operated by Nabux; it is not a Meta-authored MCP server.

Production target: **https://ig.nabuxai.com**  
MCP endpoint: **https://ig.nabuxai.com/mcp**

## Architecture

A user visits `ig.nabuxai.com`, authorizes an Instagram **Business or Creator** account through Meta's Instagram Business Login, and may connect additional official accounts to the same Nabux owner. Meta access tokens are encrypted at rest and automatically refreshed. Separately, an owner can opt in to a private session reader from the dashboard. It stores the imported session settings encrypted and exposes Direct inbox reads only.

ChatGPT, Claude, Cursor, Claude Code, and other MCP clients connect to `/mcp` using OAuth 2.1 + PKCE. The AI client receives a Nabux MCP token, **not** the Meta access token. Protected-resource metadata is served at both `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-protected-resource`. An unauthenticated `POST /mcp` returns 401 with `WWW-Authenticate` pointing at the resource metadata.

```text
Instagram Business/Creator
        │
        │ Meta official OAuth / Instagram Business Login
        ▼
https://ig.nabuxai.com
        │
        ├── encrypted Meta tokens + multi-account mapping
        ├── /mcp  ─────────────► ChatGPT / Claude / Cursor / MCP clients
        └── /internal/v1/... ──► NabuGen and trusted Nabux services

Instagram operations ─────────► graph.instagram.com (Meta official API)
Private inbox reads ──────────► isolated session account store (read-only)
```

The official Meta account store and all publish/send handlers remain unchanged. Private session accounts live in a separate table and are not eligible for official publishing or sending. The private reader uses the unofficial `instagrapi` session API, and Instagram can reject the cookie or restrict the account. Use a dedicated account if that risk is unacceptable. No private send, reaction, request-approval, mark-read, or thread-mutation tool is exposed.

## MCP tools

### Accounts & content
- `instagram_accounts`
- `instagram_profile` — reads the professional profile and stores the username when Meta returns one
- `instagram_account_refresh` — same read, for a row whose username is still empty
- `instagram_list_media`
- `instagram_publishing_limit`
- `instagram_publish_image`
- `instagram_publish_carousel` — 2 to 10 children, images and videos. A video child is `media_type=VIDEO` and is waited on until `FINISHED` before the parent is published.
- `instagram_publish_reel` — public HTTPS `video_url`; a staged MP4 URL from this gateway works.
- `instagram_publish_story` — one public HTTPS image or video URL; a staged JPEG or MP4 URL works.
- `instagram_stage_image` — stage Base64 JPEG/PNG/WebP bytes; returns opaque temporary URL and ID.
- `instagram_publish_staged_image` — publish one staged image, clean it after success.
- `instagram_publish_staged_carousel` — publish 2–10 staged images in order and clean them after success.

### Content calendar
These publish on this gateway. They do not need Postiz.

- `instagram_schedule` — queue `feed_image`, `carousel`, `reel`, `story_image`, `story_video`, or `text_card`. `account` is optional (the default account). Pass `target_id` instead of `account` to hold the post until that exact Instagram account is connected. `media_urls` and `media_ids` (library files) both work. `scheduled_at` is ISO-8601; an offset is stored as UTC, and a naive value uses `timezone` or the owner's timezone (`Asia/Muscat` until they set one).
- `instagram_list_scheduled` — filter by `status` and a `start`/`end` date range. Each row includes `attempt_log` and `last_error`.
- `instagram_update_scheduled` — change caption, time, media, or type while the post is still scheduled.
- `instagram_cancel_scheduled`
- `instagram_publish_scheduled_now`

`text_card` draws the caption onto a 1080×1350 image (1080×1920 when `frame` is `story`) with a font that includes Persian and Arabic, then publishes that image. The full text is also the caption. A background worker publishes due posts once, with a database lease so a restart or a second process does not publish the same row twice. A post more than six hours late is marked failed with `missed`. Transient errors retry with backoff up to `SCHEDULE_MAX_ATTEMPTS` (default 5). Every attempt's error is stored on the row and shown by `instagram_list_scheduled` and the calendar day view. A post aimed at a target that is not connected yet stays `awaiting_account` and is never marked missed and never published to another account.

`/calendar` is the month view. `/calendar/new` is the schedule form.

### Comments
- `instagram_get_comments`
- `instagram_reply_comment`
- `instagram_hide_comment`
- `instagram_delete_comment`

### Inbox / Messaging
- `instagram_list_conversations` — live, from Meta (first page)
- `instagram_get_conversation`
- `instagram_get_message`
- `instagram_send_message`
- `instagram_send_media`

### Optional private-session inbox (read-only)
- `instagram_private_accounts`
- `instagram_private_conversations`
- `instagram_private_thread`

The private tools read one-to-one Direct threads, including message requests, through the imported session. The dashboard accepts the cookie only over the authenticated, CSRF-protected HTTPS session; the cookie is encrypted at rest and never returned by an MCP tool. The validated session settings remain saved while the account is connected, so repeated reads do not require another login; disconnect the account to erase the saved settings. Reads reserve a persistent per-account slot at least five seconds apart, including calls from separate MCP workers. The thread tools keep a resumable cursor so historical reads can continue in bounded pages. These tools cannot send, react, approve a request, mark a thread read, or edit a conversation.

Meta's official Send API is conversation-based: it is not a cold-DM/spam API. A recipient must be eligible under Meta's messaging rules (normally they have already messaged/interacted with the professional account).

### Conversation archive

Meta's Conversations API cannot return a full history. It exposes only the **20 most recent messages** of a thread, and never returns Requests-folder threads idle for **30 days**. A complete history therefore cannot be fetched on demand — it has to be accumulated, which is what this gateway does:

- `instagram_backfill_history` — walk every conversation Meta still returns and archive what it holds. A bounded snapshot of the past, subject to the limits above.
- `instagram_conversations` — list archived threads, most recently active first. Reads the local archive, so it sees the whole recorded inbox rather than one page.
- `instagram_thread` — read one thread with a person, oldest first, paging back with `before`.

Every verified webhook delivery is archived **before** Meta is answered, for the same reason the relay queue is: Meta does not redeliver what it has acked, and no later API call can fetch the message back. Both directions are kept, so threads read as conversations rather than monologues. Run the backfill once per account at connect time; from then on the live stream keeps the archive complete.

### Insights
- `instagram_account_insights`
- `instagram_media_insights`

Metrics are passed through to Meta so the MCP can follow metric-specific requirements as the Graph API evolves.

Every tool resolves `account` only inside the authenticated user's own connected accounts. `account` can be a 1-based position, `@username`, Instagram user id, or the Nabux Account Key.

## Multi-account dashboard

`/dashboard` shows every connected Instagram account and an opaque `Account Key`, for example:

```text
ig_Q9m...random...
```

This is not a Meta token. It is a random identifier that trusted Nabux services can use to address an account through the gateway. Raw Meta tokens never leave this service.

The first connected account is the default for MCP calls that omit `account`. Additional Instagram accounts can be connected from the dashboard.

## Appearance

User-facing pages share one stylesheet, `app/static/nabux.css`, served at `/static/nabux.css`. Other Nabux MCP dashboards can reuse that file. It follows a minimal Swiss layout: one violet primary action, self-hosted DM Sans and Space Grotesk (no third-party font host), and a right-to-left page when the browser language is Persian or Arabic.

## Sign in with NabuAuth

The dashboard owner is a NabuAuth user (`https://auth.nabuxai.com`), the same account NabuCRM, NabuPilot and NabuGate use. Every Instagram account connected while that user is signed in is attached to them. The three MCP connectors resolve to that same user, so they share the accounts instead of each creating an owner.

Register this client in NabuAuth `apps.yaml`, then redeploy NabuAuth (clients load at boot):

```yaml
  - id: instagram-mcp
    name: Instagram
    description: Instagram accounts for Nabux MCP clients
    url: https://ig.nabuxai.com
    redirect_uris:
      - https://ig.nabuxai.com/auth/nabu/callback
    scopes: [openid, profile, email]
    secret_env: NABUAUTH_SECRET_INSTAGRAM_MCP
```

Generate one secret (`openssl rand -base64 32`). Set it on the NabuAuth host as `NABUAUTH_SECRET_INSTAGRAM_MCP` and on this gateway as `NABUAUTH_CLIENT_SECRET`. The redirect URI is matched exactly.

```bash
NABUAUTH_URL=https://auth.nabuxai.com
NABUAUTH_CLIENT_ID=instagram-mcp
NABUAUTH_CLIENT_SECRET=<the same secret>
NABUAUTH_REDIRECT_URI=https://ig.nabuxai.com/auth/nabu/callback
```

Leave `NABUAUTH_CLIENT_ID` empty only as a fallback: the old per-browser owner is used, and each connect without a cookie can still create an owner.

Log out clears this gateway's session cookie and sends the browser to `GET https://auth.nabuxai.com/oauth/logout` (`end_session_endpoint`) with `post_logout_redirect_uri` set to `https://ig.nabuxai.com/signed-out`. NabuAuth allows that redirect when the origin matches the app's `url` or any `redirect_uris` entry, so the `url: https://ig.nabuxai.com` line above is the field that covers it. There is no separate post-logout list. `/oauth/logout` does not read `id_token_hint`. Logging out does not delete Instagram accounts or tokens.

The first time someone signs in, the browser's existing owner (the one in the session cookie, with no Nabu identity yet) is adopted, tokens included. Other accounts connected before sign-in are listed once on the dashboard as "Add to my account". Adding one moves the encrypted token onto this login. An account that already belongs to a different Nabu user is left where it is.

## Connect on a phone

`/connect` shows the Instagram authorize link on a full page. It does not open a popup and it does not depend on `window.opener`. Use **Continue** for a normal browser. If the Instagram app opens the link and shows no Allow button, copy the link into Chrome or Safari.

Instagram sends the browser back to `GET /instagram/callback?code=...&state=...` (POST is accepted as well). The `state` value is an HMAC signed with `APP_SECRET_KEY` and carries the owner id and the MCP transaction id, so the flow can start in one browser and finish in another without the session cookie. The signature is single-use. MCP transactions are stored in SQLite, not only in process memory, so another worker can finish the redirect back to the AI client.

The page you land on is a success or error page. When an AI client is waiting, it also links back to that client's redirect URI. The account is saved before that hop, so a phone that cannot open `cursor://` or `localhost` still keeps the connection.

A Graph error that says `Unsupported request - method type: get` comes from Meta, after this server has already accepted the browser return. The usual cause is Access verification on the Meta app (role-less accounts) or the long-lived token exchange. The account is still saved when the code exchange returned a user id. Confirm:

- Redirect URI is exactly `https://ig.nabuxai.com/instagram/callback`
- The Meta app is Live and Access verification (Tech Provider) is approved
- `IG_APP_ID` / `IG_APP_SECRET` are the Instagram app id and secret, not only the Facebook app id

## Cursor connectors

`Instagram-Nabux`, `nabux-instagram`, and `ig-nabux-zoey` are three MCP clients of this one server. They are not three codebases. All three use:

```text
https://ig.nabuxai.com/mcp
```

OAuth issues a Nabux token for the owner who approved Instagram. Reconnecting an Instagram user id stays on the owner that already has it, so a second connector sees the same accounts when the same professional account is approved. Pass `account` as a position, `@username`, Instagram user id, or Account Key.

To pin one connector to one account without sharing a browser session, open `/dashboard` and generate a connector URL. Paste it into that connector as the MCP URL:

```text
https://ig.nabuxai.com/t/<token>/mcp
```

The URL is a secret and is shown once. Calls that omit `account` use the account you picked for that connector. Existing OAuth tokens are not revoked. `nabux-instagram` is the name in `mcp.json`. `ig-nabux-zoey` is the same server, meant to be pinned to the Zoey account.

## Postiz

Postiz cannot store an Instagram token through its public API. What it does support is an OAuth app that hands this server a `pos_` token, plus `GET /public/v1/social/instagram-standalone`, which returns Postiz's own Instagram authorize URL.

In Postiz open **Settings → Apps → Create OAuth App** and enter:

| Field | Value |
| --- | --- |
| App Name | `Nabux Instagram` |
| Description | Schedule Instagram posts from ig.nabuxai.com through this Postiz organization. |
| Profile picture | optional |
| Redirect URL | `https://ig.nabuxai.com/postiz/callback` |

On the dashboard, open **Advanced** and save that login's Postiz address, client id (`pca_…`), and client secret (`pcs_…`). The secret is encrypted with `APP_SECRET_KEY` and the page shows only the last four characters. Leave the API address empty to use the Postiz address plus `/api`. Postiz Cloud uses `https://platform.postiz.com` and `https://api.postiz.com`. Then choose **Connect Postiz**. The `pos_` token is encrypted the same way and is not shown again.

Server env vars are only a fallback for a login that has not saved an app:

```bash
POSTIZ_FRONTEND_URL=https://<your-postiz-origin>
POSTIZ_API_URL=https://<your-postiz-origin>/api
POSTIZ_CLIENT_ID=pca_...
POSTIZ_CLIENT_SECRET=pcs_...
```

**Add Instagram to Postiz** on the dashboard loads Postiz's authorize URL onto the same full-page handoff (copy it into a mobile browser if the Instagram app intercepts it). After Allow, the browser returns to Postiz, not to this gateway. On the Meta app, keep this gateway's redirect URI and add Postiz's as well:

```text
https://ig.nabuxai.com/instagram/callback
https://<your-postiz-origin>/integrations/social/instagram-standalone
```

Set Postiz's `INSTAGRAM_APP_ID` and `INSTAGRAM_APP_SECRET` to the same Meta app as `IG_APP_ID` and `IG_APP_SECRET`.

MCP tools `instagram_postiz_status` and `instagram_schedule_post` are optional. They use the stored organization token when a post should live in Postiz. The gateway's own calendar (`instagram_schedule` and `/calendar`) does not use Postiz. Postiz scheduling defaults to a draft. Media must be a staged URL from this gateway; the server uploads those bytes to Postiz and does not fetch arbitrary URLs.

The dashboard also has a separate **Private session reader** section. It accepts a browser `sessionid` cookie for optional read-only Direct imports. The private account gets a separate `igp_…` key and can be mapped to a NabuCRM business with `channel-bridge:link-instagram-private --account=KEY --business=ID`. Never paste a private key or cookie into `NabuCRM` environment variables.

## Token lifecycle

Instagram long-lived tokens are encrypted with the gateway's `APP_SECRET_KEY`. The service performs a refresh pass every 12 hours and refreshes accounts that are within seven days of expiry. MCP calls also lazily refresh a near-expiry token before using it.

This means an actively connected account does not depend on a person manually replacing a token every few weeks.

## NabuCRM intake

Every Instagram account connected here is reported to NabuCRM as an
`instagram_connected` event (contract: NabuCRM's `docs/crm-intake.md`), so the
person lands in the Nabux CRM. The report is queued in the `crm_outbox` table
by the OAuth callback and sent by a background loop every 30 seconds, signed
with `NABUGATE_SECRET` as app `instagram-mcp`.

- 2xx: done. 5xx, 429 and network errors: retried with backoff (1 minute
  doubling to 6 hours, 24 attempts). Any other 4xx: marked failed, not retried.
- `NABUGATE_SECRET` must equal NabuCRM's. While it is empty, reports wait in
  the queue and are sent once it is set.
- `CRM_INTAKE_URL` (default `https://crm.nabuxai.com/api/v1/crm/intake`),
  `CRM_INTAKE_ENABLED` (default `true`).

```bash
python -m app.crm_intake backfill        # report accounts connected before this existed (idempotent)
python -m app.crm_intake status          # pending / done / failed counts
python -m app.crm_intake requeue-failed  # retry refused reports after fixing the cause
```

## NabuGen integration

NabuGen uses a server-to-server bearer secret (`INTERNAL_SERVICE_TOKEN`) and the Account Key. Current internal endpoints:

```text
GET  /internal/v1/accounts/{account_key}
POST /internal/v1/accounts/{account_key}/publish/image
POST /internal/v1/accounts/{account_key}/publish/carousel
POST /internal/v1/accounts/{account_key}/publish/reel
POST /internal/v1/accounts/{account_key}/publish/story
```

## Meta app setup

Create/configure a Meta developer app for **Instagram API with Instagram Login** and use Instagram Business Login. Default requested permissions:

```text
instagram_business_basic
instagram_business_content_publish
instagram_business_manage_comments
instagram_business_manage_messages
instagram_business_manage_insights
```

Set this exact OAuth redirect URI in Meta:

```text
https://ig.nabuxai.com/instagram/callback
```

Compliance callbacks:

```text
https://ig.nabuxai.com/instagram/deauthorize
https://ig.nabuxai.com/instagram/data-deletion
```

Webhook (for real-time messaging/comments subscriptions):

```text
Callback URL:  https://ig.nabuxai.com/webhook
Verify Token:  <any string you pick — must equal IG_WEBHOOK_VERIFY_TOKEN>
```

Set `IG_WEBHOOK_VERIFY_TOKEN` in your environment to that same string *before* clicking
"Verify and save" in Meta — the server only accepts the handshake when they match exactly
(no leading/trailing spaces). Payloads are additionally checked against Meta's
`X-Hub-Signature-256` header using `IG_APP_SECRET`.

This gateway shares its Meta app with other Nabux products (Zooey uses the same
`META_APP_ID`/`INSTAGRAM_CLIENT_ID`). A Meta app allows one callback per product, so
point **both** the Instagram product and the Page/Messenger product at `/webhook` here
and list the other products' own Meta webhook URLs in `META_RELAY_TARGETS`: every
verified delivery is forwarded to them byte-for-byte with Meta's original
`X-Hub-Signature-256`, so they keep verifying with their own copy of the app secret
(Page deliveries are signed with `FB_APP_SECRET`, Instagram ones with `IG_APP_SECRET`;
both are accepted here).

Once verified, subscribe the app to the `messages` and `comments` webhook fields. A
signed DM or comment for a connected account is parsed and relayed to NabuCRM
(`NABUCRM_INBOUND_URL`, authenticated with `INTERNAL_SERVICE_TOKEN`) as a lead;
NabuCRM's reply comes back through `POST /internal/v1/accounts/{account_key}/messages/send`,
the same internal-token pattern as the `publish/*` routes above. An account this
gateway doesn't recognize is a no-op — Meta still gets its `200 OK` either way.

Meta gets its `200 OK` as soon as the signature checks out and the delivery is on disk;
the relays run after it, so a slow downstream can never make Meta retry or switch the
subscription off.

**The queue is what makes that safe.** Acking first means Meta considers the delivery
made and never sends it again, so this process holds the only copy. Every verified
delivery is therefore written to the `meta_outbox` table — one row per target, before
the `200` — and `app/meta_relay.py` delivers it, retrying with backoff (10s doubling to
an hour, 18 attempts ≈ a day) until the sibling takes it. A sibling redeploying, timing
out or answering 5xx now costs latency instead of the message; only a refusal it would
repeat (a 4xx that is not 408/429) is given up on, loudly.

```text
python -m app.meta_relay status          # pending / done / failed, and the configured targets
python -m app.meta_relay requeue-failed  # try everything given up on again, once the cause is fixed
```

What the relay did is in the log:

```text
gave up relaying a Meta delivery to <url> after N attempts ...      # the one place a message is lost
META_RELAY_TARGETS is empty: ...                      # once per process: siblings get nothing
NABUCRM_INBOUND_URL is not set: ...                   # once per process: no CRM relay
rejected a webhook delivery: ... (ig_secret_set=… fb_secret_set=…)   # at most once a minute
```

A DM does not have to carry words: photos, voice notes, stickers, GIFs, shared reels and
media-only story replies arrive with `attachments` and no `text`, and reactions arrive
with neither. All of them are relayed, filed under what they are (`[photo]`,
`[voice message]`, `[reacted ❤️]`), so nothing inbound is invisible. Comments on a live
video (`live_comments`) count as comments.

Graph API defaults to `v26.0` and can be changed with `IG_GRAPH_VERSION`.

## Media library

Library files are kept until `instagram_media_delete`. Nothing expires them. They live under `MEDIA_DIR` (default `/data/media`). Coolify must mount a persistent volume there. `docker-compose.coolify.yml` mounts `instagram-mcp-data` at `/data`, so the default path is on that volume. If you set `MEDIA_DIR` somewhere else, mount that path too. Do not use the container's own disk.

The public URL is `https://ig.nabuxai.com/media/{id}/{token}.ext`. It needs no `Authorization` header. Videos are `Content-Type: video/mp4` and answer HTTP `Range` with `206`. Instagram fetches that URL itself.

Upload paths:

- `POST /api/v1/media` multipart field `file`, plus `account` or `target_id`. Authorize with the same bearer token the MCP uses (`Authorization: Bearer …`) or with the dashboard session cookie and a `csrf` field. The body is written to disk as it arrives. Default limit is 1 GiB (`MEDIA_MAX_BYTES`).
- `instagram_media_upload_url` downloads one public HTTPS URL into the library.
- `instagram_media_upload_begin` returns a one-time signed multipart URL and a curl example for a file that is already on the caller's machine. The URL works once and does not need the bearer.
- `instagram_media_list` and `instagram_media_delete`.

The same sha256 for one account (or one unbound target) is stored once. A remote URL that the calendar re-stages still uses the temporary staging cap (`STAGED_VIDEO_MAX_BYTES`, 100 MiB). A library file is not copied again; Meta fetches the stable URL, so a reel or carousel video can be the full library size.

**Reverse proxy body size.** This process does not cap the body below `MEDIA_MAX_BYTES`. Coolify's Traefik proxy does not add a limit. nginx's default `client_max_body_size` is 1m, which rejects the upload before the app sees it. Set it to at least the library limit, for example `client_max_body_size 1024m;`, on any nginx in front. Cloudflare's free upload cap is smaller than 1 GiB; a direct origin upload is required for files that large.

`ffprobe`, when it is installed, fills duration, width, and height. Images use Pillow. Without ffprobe, an MP4 still stores duration when the `mvhd` box is readable from the start of the file.

## Targets

`instagram_target_create` stores a username (lowercased, without `@`) and/or a numeric Instagram id for an account that is not connected yet. `instagram_target_list` shows them. Uploads (`POST /api/v1/media` field `target_id`, and the media MCP tools) and `instagram_schedule` accept `target_id` instead of an account. Those posts stay `awaiting_account`.

When this owner connects that account, or refreshes its profile, an exact username or exact id match binds the target. Future posts become `scheduled`. Posts whose time has already passed become `needs_review` and are not published. The dashboard lists them with **Approve schedule**, **Edit**, and **Cancel**. A different username or id, including a longer handle, is not a match, and another owner's target is never bound.

Upload a local MP4 for `@athr.omani` before that account is connected, then schedule it:

```text
instagram_target_create  username=athr.omani  label="Athr Omani"
instagram_media_upload_begin  target_id=<id from the create result>
curl -X POST -F "file=@clip.mp4" "<upload_url from the begin result>"
instagram_schedule  type=reel  target_id=<id>  media_ids=[<media id>]  scheduled_at=2026-11-05T18:00:00  timezone=Asia/Muscat  caption="..."
```

The post stays `awaiting_account` until an account whose username is exactly `athr.omani` (any capitalization) is connected by this same owner.

## Profile host

Instagram Login tokens are read from `graph.instagram.com`, `/me` first, with fields `user_id,username,name,profile_picture_url,account_type`. Facebook Login tokens (they start with `EAA`) use `graph.facebook.com`. A Meta `Unsupported request - method type: get` tries the unversioned host, the stored id, then the other host. `instagram_account_refresh` and the dashboard **Refresh profile** button write the username onto the row. Tokens are not logged.

## Graph tools ported from mcpware/instagram-mcp

Already on this gateway, so not duplicated: profile, media list, account and media insights, publishing (image, carousel, reel, story), publishing limit, comments (list, reply, hide, delete), and Instagram Login messaging (`instagram_list_conversations`, `instagram_get_conversation`, `instagram_send_message`, `instagram_send_media`). Those DMs use `graph.instagram.com`. mcpware's DM tools go through a Facebook Page id on `graph.facebook.com`; replacing the working path would drop Instagram Login inboxes.

Added:

- `instagram_post_comment` — top-level comment. This gateway already had replies.
- `instagram_stories`
- `instagram_mentions` — the `tags` edge
- `instagram_hashtag_search` and `instagram_hashtag_media` — Meta's 30 unique hashtags per 7 days still applies. Instagram Login often cannot call `ig_hashtag_search`; the token's host is tried first and Meta's error is returned.
- `instagram_business_discovery` — public profile of another Business or Creator account. The handle is passed only as `business_discovery.username(handle)`.

Not ported:

- `get_account_pages` — needs a Facebook user token that administers a Page. Instagram Login, which this gateway uses, has no Page list.
- `validate_access_token` — `instagram_profile` and `instagram_account_refresh` already read `/me`. A second tool would only encourage handling the token. The token is never returned.
- Their single `INSTAGRAM_ACCESS_TOKEN`, in-memory cache, prompts, and MCP resources — this gateway is multi-owner and keeps Meta tokens encrypted.

## Coolify deployment

Use `docker-compose.coolify.yml` and assign the `mcp` service:

```text
https://ig.nabuxai.com  -> container port 8000
```

Required environment variables:

```bash
PUBLIC_BASE_URL=https://ig.nabuxai.com
APP_SECRET_KEY=<random 32+ char secret>
IG_APP_ID=<Meta app id>
IG_APP_SECRET=<Meta app secret>
IG_REDIRECT_URI=https://ig.nabuxai.com/instagram/callback
INTERNAL_SERVICE_TOKEN=<random server-to-server secret>
IG_WEBHOOK_VERIFY_TOKEN=<random string — same value entered in Meta's Webhooks setup>
NABUAUTH_URL=https://auth.nabuxai.com
NABUAUTH_CLIENT_ID=instagram-mcp
NABUAUTH_CLIENT_SECRET=<same value as NABUAUTH_SECRET_INSTAGRAM_MCP on NabuAuth>
NABUAUTH_REDIRECT_URI=https://ig.nabuxai.com/auth/nabu/callback
POSTIZ_FRONTEND_URL=https://<your-postiz-origin>
POSTIZ_API_URL=https://<your-postiz-origin>/api
POSTIZ_CLIENT_ID=<pca_ client id from Postiz Settings → Apps>
POSTIZ_CLIENT_SECRET=<pcs_ client secret from that app>
MEDIA_DIR=/data/media
MEDIA_MAX_BYTES=1073741824
SCHEDULE_MAX_ATTEMPTS=5
```

`POSTIZ_*` is optional. Leave it unset until the OAuth app exists. Changing `APP_SECRET_KEY` makes stored Meta tokens and the Postiz token unreadable; do not rotate it to turn this feature on.

`MEDIA_DIR` defaults to `/data/media` on the `instagram-mcp-data` volume mounted at `/data`. That volume has to be persistent. `MEDIA_MAX_BYTES` defaults to 1 GiB. If an nginx proxy is in front of Coolify, set `client_max_body_size 1024m;` or the upload never reaches the app. Traefik does not apply a body limit.

## Connect ChatGPT / Claude / MCP clients

Use:

```text
https://ig.nabuxai.com/mcp
```

The server supports MCP OAuth 2.1 with dynamic client registration and PKCE. For clients that cannot perform OAuth, `/dashboard` can generate a personal MCP bearer token.

## Security model

- Meta tokens are encrypted at rest using a Fernet key derived from `APP_SECRET_KEY`.
- MCP access/refresh tokens are stored only as SHA-256 hashes.
- Account resolution is tenant-scoped.
- NabuGen has a separate server-to-server bearer secret.
- Private session settings are encrypted with `APP_SECRET_KEY` in a separate account table; disconnecting clears the stored session locally.
- Private tools use only inbox-list and thread-read calls. The existing official publish, send, and webhook routes do not resolve private account keys.
- Private cookies can be rejected or trigger account restrictions; this mode is optional and should use an account selected for that risk.


## Temporary media staging (no permanent public media hosting)

Instagram's official publishing API fetches images and videos from a public
HTTPS URL; a local chat file path or `sandbox:/` URL is **not** accessible to
Meta. This gateway supports temporary, opaque HTTPS URLs under
`https://ig.nabuxai.com/media/{staged_id}.jpg` and
`https://ig.nabuxai.com/media/{staged_id}.mp4`.

- Authenticated MCP clients call `instagram_stage_image` with actual image
  bytes encoded as Base64 (or a standard image data URL). This step requires
  the MCP host/connector to forward attachment bytes. A text-only connector
  **cannot automatically read ChatGPT-generated files** merely by adding a
  schema field; connect an attachment-byte bridge if your host does not expose
  them.
- Trusted Nabux backends can HTTP POST *raw image bytes* to
  `/internal/v1/accounts/{account_key}/stage` with
  `Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` and the matching
  `Content-Type: image/png` (or `image/jpeg`, `image/webp`).
  The result has `staged_id`, `image_url`, and `expires_at`.
- Publish using the MCP tools `instagram_publish_staged_image` or
  `instagram_publish_staged_carousel`, or post `{"staged_id":"..."}`
  / `{"staged_ids":["...","..."]}` to the existing internal
  `publish/image` / `publish/carousel` endpoints.
- Staged images are converted to JPEG for Meta, limited to 8 MiB input
  and 25 megapixels, scoped to a specific owner and Instagram account, and
  restricted to 40 outstanding staged files per account (images and videos
  share this cap).
- Media URLs are bearer URLs. They are publicly fetchable by anyone with
  the opaque URL during their validity, so never stage secret or private
  material without consent. Paths cannot be listed.
- The server checks Meta's ingestion status, publishes, and **deletes the
  temporary image files only after Meta returns a published media ID**. Failed
  requests keep images for retries until the expiry janitor removes them.
  Default TTL is 4 hours (`STAGED_MEDIA_TTL=14400`); cleanup runs every
  5 minutes and after service restart.
- Images reside under `DATA_DIR/staged-media`. The existing Coolify
  `instagram-mcp-data:/data` volume persists them across restarts. Do
  not add the staged directory to backups.

### Reels and video Stories (MP4)

The same stage route accepts `video/mp4` and `video/quicktime`. The body is
streamed to disk (not re-encoded), checked for an ISO BMFF `ftyp` box, and
published at `video_url`. QuickTime uploads must already be `ftyp` files; they
are served as `video/mp4` from the `.mp4` URL. Instagram still requires a
codec it can ingest (typically H.264 + AAC); this gateway does not transcode.

```bash
curl -X POST -H "Authorization: Bearer $INTERNAL_SERVICE_TOKEN" -H "Content-Type: video/mp4" --data-binary @reel.mp4 https://ig.nabuxai.com/internal/v1/accounts/<account_key>/stage
```

The JSON result has `staged_id`, `video_url`, and `expires_at`. Pass
`video_url` to `instagram_publish_reel` or `instagram_publish_story`, or POST
`{"video_url":"<video_url>"}` to
`/internal/v1/accounts/{account_key}/publish/reel` or `publish/story`.

`GET /media/{staged_id}.mp4` is public: no `Authorization` header and no
redirect. The response is `Content-Type: video/mp4` with `Content-Length` and
`Accept-Ranges: bytes`, including `206` for a `Range` request. After
`STAGED_MEDIA_TTL` the URL is `404`. Unlike staged images, the file is kept
until that expiry (not deleted when publish returns) so Meta can finish
fetching it.

Default cap is 100 MiB (`STAGED_VIDEO_MAX_BYTES=104857600`). Videos share the
40 outstanding staged files per account with images. Set
`STAGED_VIDEO_MAX_BYTES` and `STAGED_MEDIA_TTL` in the deploy environment to
override either limit; both have defaults, so an existing deploy keeps working
without new variables.

The existing URL-based publishing tools remain backward compatible. This
feature does not authorize publication without a user action or solve
attachment forwarding outside the Nabux service.
