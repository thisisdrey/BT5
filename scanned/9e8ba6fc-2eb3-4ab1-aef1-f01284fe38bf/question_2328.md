# Q2328: write-feed via supply-collateral-add: make two code sites that must agree disagree by an attacke

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the position state the final collateral-add is validated against reach `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it applies one Pyth price-feed update and folds its status, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `supply-collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the position state the final collateral-add is validated against across its boundary values through `supply-collateral-add` in simnet and assert `write-feed` never returns a value that breaks the invariant.
