# Q4226: resolve-dia via collateral-remove-redeem: satisfy a bound with a value the bound was never designed 

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling remaining zToken collateral whose price moves with the redeem, can an unprivileged attacker make `resolve-dia` (mainnet/contracts/market/v0-4-market.clar:326) satisfy a bound with a value the bound was never designed to admit? `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident, so the invariant that every asset a position holds enters the health evaluation exactly once would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:326` -> `resolve-dia`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: remaining zToken collateral whose price moves with the redeem
- Exploit idea: `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident. Reach it through `collateral-remove-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with remaining zToken collateral whose price moves with the redeem varied, and assert that the value `resolve-dia` returns is identical in both runs; a divergence confirms the finding.
