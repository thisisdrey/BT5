# Q1980: get-egroup via supply-collateral-add: make two code sites that must agree disagree by an attacke

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls `min-shares` (the only slippage bound on the deposit leg) reach `get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it resolves the efficiency group for a mask and is unwrapped with `try!` on every health path, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `min-shares` (the only slippage bound on the deposit leg)
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `supply-collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `min-shares` (the only slippage bound on the deposit leg) across its boundary values through `supply-collateral-add` in simnet and assert `get-egroup` never returns a value that breaks the invariant.
