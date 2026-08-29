# Q0674: iter-lookup-collateral via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling `amount`, can an unprivileged attacker make `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) convert a rounding direction into a repeatable extraction? `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with `amount` varied, and assert that the value `iter-lookup-collateral` returns is identical in both runs; a divergence confirms the finding.
