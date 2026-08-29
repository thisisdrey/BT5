# Q2028: unwrap-status via collateral-add: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls `amount` reach `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it resolves `status` with `unwrap-panic`, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `collateral-add` in simnet and assert `unwrap-status` never returns a value that breaks the invariant.
