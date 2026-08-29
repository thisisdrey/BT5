# Q5897: resolve-interpolation-points via collateral-remove-redeem: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), controlling `receiver` for the underlying leg, drive `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) — which selects the bracketing curve points for a utilization — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `collateral-remove-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `collateral-remove-redeem` call, then the attacker-shaped one with `receiver` for the underlying leg, and assert the attacker's net token balance change is zero or negative.
