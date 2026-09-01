# Q3064: lib - wNEAR debited but NEAR never delivered (24)

## Question
Given the token contract is one the attacker deployed and can fail on demand, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, make the `near_withdraw` unwrap or the follow-on call reached from `NEAR_WITHDRAW_GAS` in `crates/near/wnear/src/lib.rs` fail after the signer's wNEAR was already subtracted, with no refund path, breaking the invariant `wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/wnear/src/lib.rs](crates/near/wnear/src/lib.rs) - `NEAR_WITHDRAW_GAS` (cross-check `near_deposit` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `native_withdraw`, `storage_deposit`, deposit-bearing `auth_call` and `FtWithdraw::storage_deposit` all debit first and document that wNEAR is not refunded on failure. Set-up: the token contract is one the attacker deployed and can fail on demand.
- Invariant to test: wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Point the receiver at a non-existent or reverting account; assert whether the wNEAR is recoverable.
