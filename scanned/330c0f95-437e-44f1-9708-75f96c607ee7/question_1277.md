# Q1277: global contract storage cost attribution — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, a maximal-size global contract deployed once and adopted by many cheap accounts, when combined with a DeployContract earlier in the same action list, reach `data` in `core/primitives/src/universal_state_init.rs` and have the deployer refunded storage staking while the code is still referenced and must be retained, breaking the invariant that whoever the protocol charges for global code storage cannot reclaim it while references exist, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives/src/universal_state_init.rs` :: `data`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: a maximal-size global contract deployed once and adopted by many cheap accounts; when combined with a DeployContract earlier in the same action list
- Exploit idea: have the deployer refunded storage staking while the code is still referenced and must be retained
- Invariant to test: whoever the protocol charges for global code storage cannot reclaim it while references exist
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test comparing charged storage against retained code across deploy/withdraw
