# Q3524: tokens - promise_result parsing accepts a wrong-length or wrong-typed vector (23)

## Question
Given the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, return a JSON array of unexpected length or element type so `MT_ON_TRANSFER_GAS_DEFAULT` in `contracts/defuse/core/src/tokens.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/core/src/tokens.rs](contracts/defuse/core/src/tokens.rs) - `MT_ON_TRANSFER_GAS_DEFAULT` (cross-check `TransferEvent` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
