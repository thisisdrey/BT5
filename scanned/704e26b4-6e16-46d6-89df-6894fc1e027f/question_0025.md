# Q0025: calc-treasury-lp-preview via deposit: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), controlling whether the vault is at a zero-supply or zero-asset edge, drive `calc-treasury-lp-preview` (mainnet/contracts/vault/v0-vault-stx.clar:350) — which divides by `(- ta-preview reserve-inc)`, a denominator that can reach zero or underflow — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:350` -> `calc-treasury-lp-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: whether the vault is at a zero-supply or zero-asset edge
- Exploit idea: `calc-treasury-lp-preview` divides by `(- ta-preview reserve-inc)`, a denominator that can reach zero or underflow. Reach it through `deposit` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `deposit` with whether the vault is at a zero-supply or zero-asset edge, then read `calc-treasury-lp-preview` state before and after in the same block and assert the two sides of the invariant are equal.
