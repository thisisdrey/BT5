# Q4049: interpolate-rate via repay: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316), controlling `amount`, including far above the real debt (the capping path), drive `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) — which interpolates between packed u16 curve points — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `repay` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `repay` call, then the attacker-shaped one with `amount`, including far above the real debt (the capping path), and assert the attacker's net token balance change is zero or negative.
