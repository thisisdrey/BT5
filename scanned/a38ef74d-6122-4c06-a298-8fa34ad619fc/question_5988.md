# Q5988: deposit - promise_result parsing accepts a wrong-length or wrong-typed vector (8)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, return a JSON array of unexpected length or element type so `mt_on_transfer` in `contracts/defuse/src/contract/tokens/nep245/deposit.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/deposit.rs](contracts/defuse/src/contract/tokens/nep245/deposit.rs) - `mt_on_transfer` (cross-check `mt_resolve_deposit` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
