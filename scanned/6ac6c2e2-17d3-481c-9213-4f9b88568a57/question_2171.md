# Q2171: tokens - promise_result parsing accepts a wrong-length or wrong-typed vector (12)

## Question
Given the named receiver account does not exist on chain, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, return a JSON array of unexpected length or element type so `MT_ON_TRANSFER_GAS_MIN` in `contracts/defuse/core/src/tokens.rs` falls back to a default that credits the wrong amounts, breaking the invariant `the refund vector applied == a well-formed response of exactly the expected length, or a conservative default` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/core/src/tokens.rs](contracts/defuse/core/src/tokens.rs) - `MT_ON_TRANSFER_GAS_MIN` (cross-check `ImtMintEvent` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `promise_result_checked_json_with_len` filters on `len()`; the `unwrap_or_else(|| amounts.clone())` fallback treats a malformed result as a full refund. Set-up: the named receiver account does not exist on chain.
- Invariant to test: the refund vector applied == a well-formed response of exactly the expected length, or a conservative default
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Return arrays of length n-1, n+1 and mixed types; assert the resolver's fallback never over-credits.
