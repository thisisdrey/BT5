# Q5880: population via collateral-remove: compose two individually correct mechanisms into an incorr

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `population` (mainnet/contracts/registry/v0-egroup.clar:81) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it counts set bits to order the bucket search, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:81` -> `population`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `population` counts set bits to order the bucket search. Reach it through `collateral-remove` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the set of assets held across its boundary values through `collateral-remove` in simnet and assert `population` never returns a value that breaks the invariant.
