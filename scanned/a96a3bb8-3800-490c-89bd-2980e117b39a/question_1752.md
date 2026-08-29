# Q1752: iter-lookup-collateral via liquidate-multi: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls the full batch list and its ordering reach `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) in a state where it make an aggregate and its per-item breakdown disagree? Given that it skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `liquidate-multi` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the full batch list and its ordering across its boundary values through `liquidate-multi` in simnet and assert `iter-lookup-collateral` never returns a value that breaks the invariant.
