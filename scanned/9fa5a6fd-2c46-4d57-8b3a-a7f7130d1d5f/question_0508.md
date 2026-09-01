# Q0508: lib - wNEAR debited but NEAR never delivered (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, make the `near_withdraw` unwrap or the follow-on call reached from `near_deposit` in `crates/near/wnear/src/lib.rs` fail after the signer's wNEAR was already subtracted, with no refund path, breaking the invariant `wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/wnear/src/lib.rs](crates/near/wnear/src/lib.rs) - `near_deposit` (cross-check `NEAR_WITHDRAW_GAS` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `native_withdraw`, `storage_deposit`, deposit-bearing `auth_call` and `FtWithdraw::storage_deposit` all debit first and document that wNEAR is not refunded on failure. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: wNEAR debited from an account == NEAR that reached the named receiver, or was returned to that account
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Point the receiver at a non-existent or reverting account; assert whether the wNEAR is recoverable.
