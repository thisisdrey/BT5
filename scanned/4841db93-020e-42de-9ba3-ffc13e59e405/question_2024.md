# Q2024: resolve-pyth via collateral-remove-redeem: make two code sites that must agree disagree by an attacke

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `amount` used for BOTH the collateral removal and the share redemption reach `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it reads the Pyth storage record for a 32-byte ident, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `amount` used for BOTH the collateral removal and the share redemption
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `collateral-remove-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with `amount` used for BOTH the collateral removal and the share redemption varied, and assert that the value `resolve-pyth` returns is identical in both runs; a divergence confirms the finding.
