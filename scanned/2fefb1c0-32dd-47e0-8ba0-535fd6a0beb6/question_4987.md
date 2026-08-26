# Q4987: Stake action with zero or dust amounts — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `install_universal_account` in `runtime/runtime/src/universal_account_id.rs` and get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked, breaking the invariant that locked balance always equals the stake the protocol accounts for, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/universal_account_id.rs` :: `install_universal_account`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked
- Invariant to test: locked balance always equals the stake the protocol accounts for
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting amount+locked conservation across Stake actions
