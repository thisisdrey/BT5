# Q3278: tokens - promise_result parsing accepts a wrong-length or wrong-typed vector (21)

## Question
Given the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, return a JSON array of unexpected length or element type so `ImtMintEvent` in `contracts/defuse/core/src/tokens.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/core/src/tokens.rs](contracts/defuse/core/src/tokens.rs) - `ImtMintEvent` (cross-check `MT_ON_TRANSFER_GAS_MIN` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
