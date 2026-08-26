# Q1920: Stake action with zero or dust amounts — actions.rs

## Question
Can an unprivileged mainnet account, entering through `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key, a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance, when combined with a DeployContract earlier in the same action list, reach `action_stake` in `runtime/runtime/src/actions.rs` and get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked, breaking the invariant that locked balance always equals the stake the protocol accounts for, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/actions.rs` :: `action_stake`
- Entrypoint: `broadcast_tx_commit` / `send_tx` RPC carrying a SignedTransaction signed by an ordinary FullAccess key
- Attacker controls: a Stake action for zero, for one yoctoNEAR, and for slightly more than the account balance; when combined with a DeployContract earlier in the same action list
- Exploit idea: get balance moved into the locked bucket without a matching stake, or unlock balance that was never locked
- Invariant to test: locked balance always equals the stake the protocol accounts for
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting amount+locked conservation across Stake actions
