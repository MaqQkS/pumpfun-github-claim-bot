# pump.fun GitHub (social) fee claims: research (2026-10-14)
All times America/Chicago (CT). Verified = pulled from mainnet RPC (api.mainnet-beta.solana.com) by me.

## 1. How GitHub recipients are represented (verified against IDL + chain)
- Program: Pump Fees `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` (IDL: github.com/pump-fun/pump-public-docs idl/pump_fees.json; SDK @pump-fun/pump-sdk 4.1.0).
- Token's creator fees -> `SharingConfig` PDA (up to 8-10 shareholders, bps sum 10000). A shareholder can be a **SocialFeePda** = vault per GitHub user.
- SocialFeePda seeds: `["social-fee-pda", utf8(github_numeric_user_id), u8 platform]`, platform GitHub = **2** (SDK enum Pump=0,X=1,GitHub=2; DeepWiki saying 0 is wrong). ID from api.github.com/users/<login>.id.
- **One PDA per GitHub user, shared across ALL tokens that name that user.** Not per token.
- Account layout: disc(8) bump u8, version u8, user_id string, platform u8, total_claimed u64, last_claimed u64 (unix), total_stable_claimed u64, reserved[120].
- Claims are custodial-ish: user logs into pump.fun with GitHub; pump.fun's `social_claim_authority` (`2sMrGNK8i36YRkF5WWCwnaUYuwDJhHe1g2xA8aPvhkjM`, in fee_program_global `CHqnuT...`) co-signs; SOL goes to the user's chosen recipient wallet.
- Instructions (discriminators): claim_social_fee_pda e115fb85a11ec7e2, claim_social_fee_pda_v2 114df0863abc3595 (current), create_social_fee_pda 90e03bd34ef8cadc. Event `SocialFeePdaClaimed` disc 3212c141edd2eaec emitted via Anchor event CPI (inner ix data = e445a52e51cb9a1d + event disc + borsh). Fields: timestamp,user_id,platform,social_fee_pda,recipient,social_claim_authority,amount_claimed,claimable_before,lifetime_claimed,recipient_balance_before/after,quote_mint,lifetime_stable_claimed.
- Before claiming, pump.fun's app calls pump `distribute_creator_fees` (disc a572670079cef751, accounts mint,bonding_curve,sharing_config,creator_vault,...,+social PDA as remaining acct) for the token, in a separate tx ~2-5 s earlier (or same tx for the first claim). **That ix is how you tie a claim to a token.**
- pump.fun frontend-api-v3 `/coins/<mint>` returned 404 (route changed); no public token->GitHub API verified. Mapping can be derived on-chain from SharingConfig shareholders (unverified decoding, todo).

## 2. Example: A2yA6AFtfKkfVkfsnGpvsKoUV5kQPpMYboZsBPvmkA9T / GitHub obie (id 3908)
- SocialFeePda: `H75T9GTggo3vqMqYYkT3tSYJmZa3TWrPvTYYnTpbpXhP` (exists, total_claimed 276.411 SOL as of 9:5x PM CT).
- 01:06:12 AM CT `5TEpsj4e...` signer 7GhWwhaM: create_social_fee_pda + create_fee_sharing_config + update_fee_shares_v2 for A2yA6 (sharing_config BJESiawPoLnMEx1cTVFvda2AjNuhe4qGRgPmYJ8ZT1iv). Same PDA also set as recipient by ≥6 other tokens/wallets (6h7e5Pe...pump, etc.) that day.
- Claim recipient wallet (obie's): `8rCjoeuCkxZ7tT3dSJ1F8pRKZMaokKTY6gqRTCfdN3VG`.
| CT time 10/14 | sig | ix | amount | lifetime |
|---|---|---|---|---|
| 07:01:10 AM | 3yWqpMpiKK2gCf1Z7KdUsNiP266ug57bn7onLqNSShFuLKb2ToWnXMMDUQLacbRxU8EXshMRwdDadvdkfr7dUSYC | distribute_creator_fees(A2yA6) + claim_social_fee_pda (FIRST) | 110.282 SOL | 110.282 |
| 01:25:16 PM | YikcAVKVLQdP7SN8mofXxzPMQh8rocFAQj1wqS74N3sHFatjtVtoN9NKncurK7tgczshiz5THi1P7zxAEeoAE1H | claim_v2 (distribute in 27UuWt... 5s earlier) | 132.532 | 242.814 |
| 06:10:59 PM | 5MYarMfZEWkJdABQHzW13xc5LrdWty1eXPiKpev7kLyCRg2MgQbuqCiLgwFxc2sZ24T46ZD8SGZP3zW49mZWqztY | claim_v2 | 19.923 | 262.736 |
| 08:16:53 PM | 392HYYtXrVaM1a4aoa1nxD27Q1uvrX3vrSfBKRwrw6sthRzSeyQ4to6ei8w2DK21NtTzno2YuUobXth8o8nZuny2 | claim_v2 (distribute 3X8NLy... 08:16:48) | 13.675 | 276.411 |
- Every distribute before a claim was for A2yA6 -> obie picked A2yA6 as canonical token.

## 3. Detecting a FIRST claim
- First claim of a GitHub user = SocialFeePdaClaimed event where `lifetime_claimed == amount_claimed` (equivalently claimable_before... / PDA total_claimed was 0). Deterministic, no DB needed (keep DB anyway to dedupe).
- Token attribution: in the same tx (first claim) or the preceding tx by the same recipient, find pump `distribute_creator_fees` whose remaining accounts include the social PDA -> mint. Also "first claim for this token" = first distribute+claim pair for (mint, user).
- Enrich: user_id -> GitHub login via api.github.com/user/<id> (unauth 60 req/h; token 5000/h).
- Real-time: `logsSubscribe {mentions:[pfeeUx...]}` (claims are low-volume) -> getTransaction -> decode inner event CPI. Or Helius enhanced websocket `transactionSubscribe` accountInclude pfee / Helius webhook; or Yellowstone gRPC for lowest latency. Public mainnet RPC rate-limits websockets/getTransaction; recommend Helius free tier (≈1M credits/mo, enough) or paid Developer for reliability. Also catch claims where pump.fun bundles distribute in same tx.
- Optional pre-signal: watch create_social_fee_pda / update_fee_shares to list tokens assigning fees to notable GitHub users (followers count) and pre-compute PDAs.

## 4. Open source
- pump-fun/pump-public-docs (IDLs, README social fee section), @pump-fun/pump-sdk (socialFeePda helper, decodeSocialFeePdaClaimedEvent).
- nirholas/pumpkit docs/guides/social-fees.md (SDK guide; not reviewed for a watcher). No existing first-claim alert bot verified.

Scripts here: inspect.py (decodes PDA + claim events), pda_sigs.json.
