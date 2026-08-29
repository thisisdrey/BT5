# Q4590: iter-lookup-collateral via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling the position's existing collateral and debt composition, can an unprivileged attacker make `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) satisfy a bound with a value the bound was never designed to admit? `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the position's existing collateral and debt composition across its boundary values through `collateral-add` in simnet and assert `iter-lookup-collateral` never returns a value that breaks the invariant.
