# Q3756: resolve-interpolation-points via supply-collateral-add: reach a state the guard immediately upstream of it never c

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls vault share price at the moment of the deposit leg reach `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it selects the bracketing curve points for a utilization, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: vault share price at the moment of the deposit leg
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `supply-collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz vault share price at the moment of the deposit leg across its boundary values through `supply-collateral-add` in simnet and assert `resolve-interpolation-points` never returns a value that breaks the invariant.
