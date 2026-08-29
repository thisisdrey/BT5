# Q2224: next-index via deposit: make two code sites that must agree disagree by an attacke

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `amount` reach `next-index` (mainnet/contracts/vault/v0-vault-stx.clar:379) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it returns the stale `index` unchanged when the accrue pause state is set, instead of reverting, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:379` -> `next-index`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `next-index` returns the stale `index` unchanged when the accrue pause state is set, instead of reverting. Reach it through `deposit` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `deposit` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
