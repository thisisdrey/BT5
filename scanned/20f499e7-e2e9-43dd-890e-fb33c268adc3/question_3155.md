# Q3155: tokens - promise_result parsing accepts a wrong-length or wrong-typed vector (20)

## Question
Given the token contract is one the attacker deployed and can fail on demand, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, return a JSON array of unexpected length or element type so `MT_ON_TRANSFER_GAS_MIN` in `contracts/defuse/core/src/tokens.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/core/src/tokens.rs](contracts/defuse/core/src/tokens.rs) - `MT_ON_TRANSFER_GAS_MIN` (cross-check `ImtMintEvent` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the token contract is one the attacker deployed and can fail on demand.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
