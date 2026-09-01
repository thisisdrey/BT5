# Q5984: withdraw - wNEAR debited but NEAR never delivered (19)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, make the `near_withdraw` unwrap or the follow-on call reached from `FT_RESOLVE_WITHDRAW_GAS` in `contracts/defuse/src/contract/tokens/nep141/withdraw.rs` fail after the signer's wNEAR was already subtracted, with no refund path, breaking the invariant `wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/withdraw.rs](contracts/defuse/src/contract/tokens/nep141/withdraw.rs) - `FT_RESOLVE_WITHDRAW_GAS` (cross-check `internal_ft_withdraw` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `native_withdraw`, `storage_deposit`, deposit-bearing `auth_call` and `FtWithdraw::storage_deposit` all debit first and document that wNEAR is not refunded on failure. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Point the receiver at a non-existent or reverting account; assert whether the wNEAR is recoverable.
