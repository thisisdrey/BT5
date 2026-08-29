# Q2544: filter-out-debt-asset via supply-collateral-add: make two code sites that must agree disagree by an attacke

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls `amount` reach `filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it rebuilds the debt list without one asset, under `as-max-len? ... u64`, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `supply-collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount` across its boundary values through `supply-collateral-add` in simnet and assert `filter-out-debt-asset` never returns a value that breaks the invariant.
