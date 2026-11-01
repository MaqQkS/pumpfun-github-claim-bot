# GitClaim webhook protocol v1 (for receivers)

GitClaim **sends** signed HTTPS `POST` requests with a JSON body to one receiver URL that you give us. You do not run GitClaim
and you get no GitClaim credentials. Any language works (Rust, TypeScript, Python, ...).

## Transport
- `POST <your https URL>` with `Content-Type: application/json; charset=utf-8` and a UTF-8 JSON object body (≤ 64 KB in practice).
- HTTPS only. Redirects are **not** followed (3xx counts as a permanent failure). Timeouts are 5 s to connect and 8 s total.
- Reply **2xx quickly** (ideally < 2 s), then process asynchronously. The response body is ignored.

## Headers
| Header | Example | Meaning |
|---|---|---|
| `X-GitClaim-Event-ID` | `gc_claim_9dca3eb5…` | Stable id; same on every retry. Use for dedupe. |
| `X-GitClaim-Event-Type` | `claim.detected` | Also in the body. |
| `X-GitClaim-Timestamp` | `1761966030` | Unix seconds when this attempt was signed (changes per retry). |
| `X-GitClaim-Version` | `1` | Signature scheme version. |
| `X-GitClaim-Signature` | `v1=5f2c…` | Lowercase hex HMAC-SHA256. May hold several comma-separated values during secret rotation. |

## Signature verification (do all steps, in order)
1. Read the **raw request body bytes** before any JSON parsing. Do not re-serialize.
2. Require all headers and `X-GitClaim-Version == "1"`, otherwise `400`.
3. Reject if `|now - X-GitClaim-Timestamp| > 300` seconds (`401`). Keep your clock NTP-synced.
4. Build `signed = "{timestamp}.{event_id}.{version}." + raw_body` (the first part ASCII, then the raw bytes).
5. `expected = "v1=" + hex(HMAC_SHA256(key = shared_secret_utf8, msg = signed))`.
6. Split `X-GitClaim-Signature` on `,`. Accept if **any** value matches `expected` using a **constant-time** compare
   (Python `hmac.compare_digest`, Node `crypto.timingSafeEqual` on equal-length buffers, Rust `subtle::ConstantTimeEq` /
   `hmac::Mac::verify_slice`). Otherwise `401`.
7. Parse JSON and require `body.event_id == X-GitClaim-Event-ID`, otherwise `400`.
8. **Dedupe**: if you already stored this `event_id`, return `200` and do nothing. Keep seen ids for ≥ 48 h (TTL 24 h plus margin).

Pseudocode:
```
signed  = ts + "." + id + "." + ver + "." + raw_body
ok      = any(constant_time_eq("v1=" + hex(hmac_sha256(secret, signed)), s.trim()) for s in sig_header.split(","))
```
A tested reference is `examples/webhook_receiver.py` (stdlib only, ~60 lines).

## Delivery semantics
- **At-least-once.** The same event can arrive more than once (lost acks, restarts). Dedupe by `event_id`.
- **No ordering guarantee.** Retries can reorder events, and `research.completed` can arrive before its `claim.detected`.
  Join on `claim_event_id`.
- Retries happen on network errors, timeouts, `408`, `425`, `429`, `500`, `502`, `503`, `504`. Backoff is exponential with jitter
  (about 2 s, 4 s, 8 s … capped at 15 min), at most 5 retries, and `Retry-After` (seconds or HTTP date, capped at 1 h) is honored on `429`/`503`.
- **No retry** on other statuses (`400`, `401`, `403`, `404`, `410`, `413`, `422`, 3xx …). Fix the problem and tell us.
- An undelivered event expires after 24 h. GitClaim keeps at most 1000 queued events and drops new ones beyond that.
- One event per token: a claim covering several qualifying coins gives several `claim.detected` events.

## Common envelope (all events)
| Field | Type | Req | Notes |
|---|---|---|---|
| `schema_version` | string | yes | `"1.0"`. Minor bumps only add optional fields; a breaking change is `"2.0"` (new event types/headers, announced first). Ignore unknown fields. |
| `event_id` | string | yes | `gc_claim_<32 hex>` / `gc_research_<32 hex>`. Deterministic from (claim tx, mint). |
| `event_type` | string | yes | `claim.detected` or `research.completed` |
| `emitted_at` | string | yes | UTC ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`, when the event was created (not per attempt) |
| `chain` | string | yes | `"solana"` |

All Solana addresses/signatures are base58 in their **original case**; never lowercase them. Unknown values are `null`.
Every string is untrusted data (token names, descriptions, AI text): escape it before rendering and never execute it.

## `claim.detected`
| Field | Type | Req | Notes |
|---|---|---|---|
| `origin` | string | yes | `"live"` (catch-up/replay is not sent by default) |
| `verified.source` | string | yes | `"gitclaim_onchain"`: decoded from the confirmed claim tx by GitClaim |
| `verified.claim.tx_signature` | string | yes | claim tx |
| `verified.claim.classification` | string | yes | `FIRST` \| `REPEAT` \| `UNKNOWN` |
| `verified.claim.classification_detail` | string\|null | | e.g. `FIRST claim ever`, `FIRST claim on this token (unverified)` |
| `verified.claim.block_time` | string\|null | | UTC ISO time from the on-chain event |
| `verified.claim.token_fee_share_lamports` / `_sol` | int/number\|null | | **This token's** share. null if unknown |
| `verified.claim.token_fee_share_attribution` | string | yes | `exact` \| `single_coin_claim` \| `unknown` |
| `verified.claim.vault_claim_total_lamports` / `_sol` | int/number\|null | | Whole vault claim; may span several coins. **Not** this token's amount |
| `verified.claim.vault_lifetime_lamports` | int\|null | | |
| `verified.claim.coins_in_claim` | int | yes | coins the vault claim touched |
| `verified.claim.qualifying_coins_in_alert` | int | yes | coins in this alert that passed GitClaim's rules |
| `verified.claim.signer_classification` | string | yes | `app_claim` \| `automated_signer` \| `third_party_trigger` \| `unknown` |
| `verified.claim.signer_tags` | string[] | yes | raw GitClaim tags |
| `verified.claim.token_inferred` | bool | yes | true if the mint was inferred rather than read from the claim's distribute ix |
| `verified.token.mint` | string | yes | |
| `verified.claimant.role` | string | yes | always `github_linked_fee_claimant` |
| `verified.claimant.creator_relationship` | string | yes | `not_verified`. The claimant is **not** asserted to be the token creator |
| `verified.claimant.github_user_id` | string\|null | | numeric id from the claim, as a string |
| `verified.claimant.github_login` | string\|null | | from GitHub's API (cached) |
| `verified.claimant.recipient_wallet` | string\|null | | wallet that received the SOL |
| `verified.claimant.fee_vault` | string\|null | | social fee PDA |
| `third_party.symbol` | string\|null | | from pump.fun/DexScreener at alert time (not verified) |
| `third_party.symbol_source` | string\|null | | |
| `third_party.market_cap_display` | string\|null | | display string such as `$56.3k`, primary token only |
| `links.*` | string\|null | | `solscan_tx`, `solscan_token`, `pump_fun`, `dexscreener`, `github_profile`, built by GitClaim from verified ids only |

## `research.completed`
Only from GitClaim's **automatic** research (Phase 6). Manual/private research and Telegram chats are never sent. Publishing makes no new AI or RPC calls.
| Field | Type | Req | Notes |
|---|---|---|---|
| `claim_event_id` | string | yes | the `claim.detected` event this belongs to |
| `claim_tx_signature` | string | yes | |
| `token.mint` | string | yes | |
| `verified.facts` | object\|null | | GitClaim facts the research used: `mint, symbol, github_login, claim_sol, claim_total_sol, claim_coins, claim_kind, tx_signature, claim_time, signer_tags` (any may be missing) |
| `third_party.label` | string | yes | reminder that this block is unverified |
| `third_party.website` | string\|null | | public https URL only |
| `third_party.links` | string[] | yes | public https URLs only |
| `third_party.description` | string\|null | | token metadata description (scrubbed, ≤ 400 chars) |
| `third_party.repos` | object[] | yes | `{name, language, stars}` from GitHub's public API |
| `ai_interpretation.label` | string | yes | "AI-generated interpretation … not verified …" |
| `ai_interpretation.narrative` | string\|null | | ≤ 800 chars |
| `ai_interpretation.risk_verdict` | string\|null | | ≤ 400 chars. Interpretation, **not** a safety verdict or trading advice |
| `ai_interpretation.key_findings` | string[] | yes | ≤ 6 items |
| `ai_interpretation.tool_references` | string[] | yes | Queen tool names used |
| `ai_interpretation.source_urls` | string[] | yes | public https URLs only |
| `research.model` / `reasoning_effort` | string\|null | | e.g. `grok-4.3` / `high` |
| `research.trigger` | string | yes | `auto` |
| `research.completed_at` | string | yes | UTC ISO |
| `research.duration_ms` | int\|null | | |
| `research.cost_usd` / `cost_source` | number\|null / string | | `measured` or `unknown` |
| `research.cached` | bool | yes | |

Samples: `tests/fixtures/webhook/claim.detected.json`, `tests/fixtures/webhook/research.completed.json`.

## Secret rotation (no code change)
1. Generate a new secret and share it out of band. GitClaim sets `WEBHOOK_SECRET_NEXT`, so every request carries two signatures.
2. You switch your verifier to the new secret (steps above already accept any matching value).
3. GitClaim moves the new value into `WEBHOOK_SECRET` and clears `_NEXT`.
