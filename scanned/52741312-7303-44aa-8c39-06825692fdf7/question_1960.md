# Q1960: total-assets-preview via deposit: make two code sites that must agree disagree by an attacke

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `min-out` reach `total-assets-preview` (mainnet/contracts/vault/v0-vault-stx.clar:341) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it re-derives a FORWARD index inside calls that have already accrued, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:341` -> `total-assets-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `total-assets-preview` re-derives a FORWARD index inside calls that have already accrued. Reach it through `deposit` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `deposit` with `min-out`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
