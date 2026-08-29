# Q1800: resolve-ztoken via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `amount` relative to the current collateral row (the removing-all branch) reach `resolve-ztoken` (mainnet/contracts/market/v0-4-market.clar:343) in a state where it make an aggregate and its per-item breakdown disagree? Given that it reads `lindex` from the market's own `index-cache` via `get-cached-indexes`, not from the vault, then multiplies the price and divides by INDEX-PRECISION, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:343` -> `resolve-ztoken`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `amount` relative to the current collateral row (the removing-all branch)
- Exploit idea: `resolve-ztoken` reads `lindex` from the market's own `index-cache` via `get-cached-indexes`, not from the vault, then multiplies the price and divides by INDEX-PRECISION. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `amount` relative to the current collateral row (the removing-all branch) across its boundary values through `collateral-remove` in simnet and assert `resolve-ztoken` never returns a value that breaks the invariant.
