# Q2874: population via collateral-add: compose two individually correct mechanisms into an incorr

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling `amount`, can an unprivileged attacker make `population` (mainnet/contracts/registry/v0-egroup.clar:81) compose two individually correct mechanisms into an incorrect result? `population` counts set bits to order the bucket search, so the invariant that no position row exists that the position mask does not represent would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:81` -> `population`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `population` counts set bits to order the bucket search. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `collateral-add` in simnet and assert `population` never returns a value that breaks the invariant.
