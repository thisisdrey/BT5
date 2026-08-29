# Q3470: total-assets via collateral-remove-redeem: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling `receiver` for the underlying leg, can an unprivileged attacker make `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) turn an accounting residue into a permanently unclosable position? `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, so the invariant that conversions never round in the user's favour in either direction would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `collateral-remove-redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with `receiver` for the underlying leg varied, and assert that the value `total-assets` returns is identical in both runs; a divergence confirms the finding.
