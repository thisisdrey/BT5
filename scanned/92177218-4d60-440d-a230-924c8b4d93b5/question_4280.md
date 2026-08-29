# Q4280: system-repay via repay: convert a rounding direction into a repeatable extraction

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `system-repay` (mainnet/contracts/vault/v0-vault-stx.clar:902) in a state where it convert a rounding direction into a repeatable extraction? Given that it splits one payment with three different formulas for `principal-reduction`, `principal-repaid` and `interest-paid`, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:902` -> `system-repay`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `system-repay` splits one payment with three different formulas for `principal-reduction`, `principal-repaid` and `interest-paid`. Reach it through `repay` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `repay` twice with whether the repaid asset is in the accrued debt list varied, and assert that the value `system-repay` returns is identical in both runs; a divergence confirms the finding.
