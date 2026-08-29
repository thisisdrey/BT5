# Q3510: process-debt-asset via liquidate: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `min-collateral-expected`, can an unprivileged attacker make `process-debt-asset` (mainnet/contracts/market/v0-4-market.clar:761) turn an accounting residue into a permanently unclosable position? `process-debt-asset` caps debt at the max liquidatable USD and converts back to tokens with `mul-div-down`, so the invariant that conversions never round in the user's favour in either direction would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:761` -> `process-debt-asset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `process-debt-asset` caps debt at the max liquidatable USD and converts back to tokens with `mul-div-down`. Reach it through `liquidate` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `min-collateral-expected` across its boundary values through `liquidate` in simnet and assert `process-debt-asset` never returns a value that breaks the invariant.
