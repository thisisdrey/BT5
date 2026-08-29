# Q5940: resolve-pyth via collateral-remove-redeem: compose two individually correct mechanisms into an incorr

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `amount` used for BOTH the collateral removal and the share redemption reach `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it reads the Pyth storage record for a 32-byte ident, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `amount` used for BOTH the collateral removal and the share redemption
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `collateral-remove-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `amount` used for BOTH the collateral removal and the share redemption across its boundary values through `collateral-remove-redeem` in simnet and assert `resolve-pyth` never returns a value that breaks the invariant.
