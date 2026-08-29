# Q5299: ubalance via deposit: reach a state the guard immediately upstream of it never c

## Question
`ubalance` (mainnet/contracts/vault/v0-vault-stx.clar:303) reads the real underlying balance, which `deposit` and `redeem` never reconcile against the `assets` var. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing the vault's supply and asset state at the moment of the call, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:303` -> `ubalance`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `ubalance` reads the real underlying balance, which `deposit` and `redeem` never reconcile against the `assets` var. Reach it through `deposit` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `deposit` with the vault's supply and asset state at the moment of the call, then read `ubalance` state before and after in the same block and assert the two sides of the invariant are equal.
