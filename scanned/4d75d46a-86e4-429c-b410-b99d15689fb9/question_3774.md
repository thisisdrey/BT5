# Q3774: iter-lookup-collateral via collateral-remove-redeem: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling `min-underlying`, can an unprivileged attacker make `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) turn an accounting residue into a permanently unclosable position? `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `min-underlying`
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `collateral-remove-redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-underlying` across its boundary values through `collateral-remove-redeem` in simnet and assert `iter-lookup-collateral` never returns a value that breaks the invariant.
