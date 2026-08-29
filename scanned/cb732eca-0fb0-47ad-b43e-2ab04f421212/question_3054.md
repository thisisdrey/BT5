# Q3054: resolve-pyth via supply-collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling `min-shares` (the only slippage bound on the deposit leg), can an unprivileged attacker make `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) turn an accounting residue into a permanently unclosable position? `resolve-pyth` reads the Pyth storage record for a 32-byte ident, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `min-shares` (the only slippage bound on the deposit leg)
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `supply-collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-shares` (the only slippage bound on the deposit leg) across its boundary values through `supply-collateral-add` in simnet and assert `resolve-pyth` never returns a value that breaks the invariant.
