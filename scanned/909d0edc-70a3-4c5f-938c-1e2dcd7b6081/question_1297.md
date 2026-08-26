# Q1297: global contract storage cost attribution — lib.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, a maximal-size global contract deployed once and adopted by many cheap accounts, when combined with a DeployContract earlier in the same action list, reach the primary handler in this file in `runtime/runtime/src/lib.rs` and have the deployer refunded storage staking while the code is still referenced and must be retained, breaking the invariant that whoever the protocol charges for global code storage cannot reclaim it while references exist, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/lib.rs` :: primary handler
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: a maximal-size global contract deployed once and adopted by many cheap accounts; when combined with a DeployContract earlier in the same action list
- Exploit idea: have the deployer refunded storage staking while the code is still referenced and must be retained
- Invariant to test: whoever the protocol charges for global code storage cannot reclaim it while references exist
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test comparing charged storage against retained code across deploy/withdraw
