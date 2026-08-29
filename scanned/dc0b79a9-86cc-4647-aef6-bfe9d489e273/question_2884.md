# Q2884: interest-rate via call-ststx-ratio: make two code sites that must agree disagree by an attacke

## Question
Does `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) let an unprivileged attacker who controls whether the ratio is fetched before or after other state changes in the block reach `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it interpolates the packed curve at the current utilization, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: whether the ratio is fetched before or after other state changes in the block
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `call-ststx-ratio` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `call-ststx-ratio` with whether the ratio is fetched before or after other state changes in the block, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
