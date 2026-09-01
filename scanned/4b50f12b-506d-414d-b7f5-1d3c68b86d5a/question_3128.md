# Q3128: state - promise_result parsing accepts a wrong-length or wrong-typed vector (5)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, return a JSON array of unexpected length or element type so `auth_call_callback_gas` in `contracts/defuse/src/contract/intents/state.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/intents/state.rs](contracts/defuse/src/contract/intents/state.rs) - `auth_call_callback_gas` (cross-check `wnear_id` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
