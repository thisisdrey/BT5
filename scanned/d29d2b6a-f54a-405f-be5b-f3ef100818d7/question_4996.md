# Q4996: Stake action with zero or dust amounts — account.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `uninitialized_account_from_serde` in `core/primitives-core/src/account.rs` and get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked, breaking the invariant that locked balance always equals the stake the protocol accounts for, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/account.rs` :: `uninitialized_account_from_serde`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked
- Invariant to test: locked balance always equals the stake the protocol accounts for
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting amount+locked conservation across Stake actions
