# Q2610: price-multi-resolve via collateral-add: compose two individually correct mechanisms into an incorr

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling `amount`, can an unprivileged attacker make `price-multi-resolve` (mainnet/contracts/market/v0-4-market.clar:397) compose two individually correct mechanisms into an incorrect result? `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end, so the invariant that no position row exists that the position mask does not represent would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:397` -> `price-multi-resolve`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `collateral-add` in simnet and assert `price-multi-resolve` never returns a value that breaks the invariant.
