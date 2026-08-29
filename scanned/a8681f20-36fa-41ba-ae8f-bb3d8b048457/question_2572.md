# Q2572: collateral-remove via collateral-remove: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `collateral-remove` (mainnet/contracts/market/v0-market-vault.clar:406) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it decrements the map and writes the entry before `send-tokens` executes, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:406` -> `collateral-remove`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `collateral-remove` decrements the map and writes the entry before `send-tokens` executes. Reach it through `collateral-remove` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-remove` with the set of assets held, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
