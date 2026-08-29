# Q5634: mask-pos via collateral-remove-redeem: make an aggregate and its per-item breakdown disagree

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling the zToken/underlying id mapping reached (the u100 sentinel branch), can an unprivileged attacker make `mask-pos` (mainnet/contracts/market/v0-market-vault.clar:91) make an aggregate and its per-item breakdown disagree? `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET, so the invariant that only the acting principal's own position is mutated would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:91` -> `mask-pos`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: the zToken/underlying id mapping reached (the u100 sentinel branch)
- Exploit idea: `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET. Reach it through `collateral-remove-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the zToken/underlying id mapping reached (the u100 sentinel branch) across its boundary values through `collateral-remove-redeem` in simnet and assert `mask-pos` never returns a value that breaks the invariant.
