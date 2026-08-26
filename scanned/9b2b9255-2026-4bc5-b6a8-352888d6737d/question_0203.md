# Q0203: DeleteAccount beneficiary loop — lib.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, two attacker accounts each naming the other as DeleteAccount beneficiary in the same block, when combined with a DeployContract earlier in the same action list, reach the primary handler in this file in `runtime/runtime/src/lib.rs` and make the transfer of remaining balance loop, drop, or double-credit, breaking the invariant that the deleted account's balance lands exactly once with the beneficiary or is refunded, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/lib.rs` :: primary handler
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: two attacker accounts each naming the other as DeleteAccount beneficiary in the same block; when combined with a DeployContract earlier in the same action list
- Exploit idea: make the transfer of remaining balance loop, drop, or double-credit
- Invariant to test: the deleted account's balance lands exactly once with the beneficiary or is refunded
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test applying reciprocal DeleteAccount actions in one chunk
