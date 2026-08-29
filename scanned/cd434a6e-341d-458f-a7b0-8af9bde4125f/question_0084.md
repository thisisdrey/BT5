# Q0084: mask-to-list-internal via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `mask-to-list-internal` (mainnet/contracts/market/v0-4-market.clar:435) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it expands mask bits into a list bounded at 64 entries, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:435` -> `mask-to-list-internal`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `mask-to-list-internal` expands mask bits into a list bounded at 64 entries. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the set of assets held across its boundary values through `collateral-remove` in simnet and assert `mask-to-list-internal` never returns a value that breaks the invariant.
