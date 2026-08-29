# Q3198: get-account-scaled-debt via liquidate: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `min-collateral-expected`, can an unprivileged attacker make `get-account-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:307) turn an accounting residue into a permanently unclosable position? `get-account-scaled-debt` reads one scaled debt row, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:307` -> `get-account-scaled-debt`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `get-account-scaled-debt` reads one scaled debt row. Reach it through `liquidate` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-collateral-expected` across its boundary values through `liquidate` in simnet and assert `get-account-scaled-debt` never returns a value that breaks the invariant.
