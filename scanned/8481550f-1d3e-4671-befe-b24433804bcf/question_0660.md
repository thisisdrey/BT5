# Q0660: insert via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls call ordering within the block reach `insert` (mainnet/contracts/market/v0-market-vault.clar:159) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it rewrites the whole registry entry for a user id, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:159` -> `insert`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: call ordering within the block
- Exploit idea: `insert` rewrites the whole registry entry for a user id. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz call ordering within the block across its boundary values through `collateral-add` in simnet and assert `insert` never returns a value that breaks the invariant.
