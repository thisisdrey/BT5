# Q0947: merge-price via supply-collateral-add: compose two individually correct mechanisms into an incorr

## Question
`merge-price` (mainnet/contracts/market/v0-4-market.clar:506) attaches a price to an asset record by position in the fold, not by asset id. Can an unprivileged caller of `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), by choosing `min-shares` (the only slippage bound on the deposit leg), use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that a value cached within a block still describes the state it was derived from and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `min-shares` (the only slippage bound on the deposit leg)
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `supply-collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `supply-collateral-add` call, then the attacker-shaped one with `min-shares` (the only slippage bound on the deposit leg), and assert the attacker's net token balance change is zero or negative.
