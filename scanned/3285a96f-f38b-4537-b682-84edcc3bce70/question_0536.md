# Q0536: refresh via repay: satisfy a bound with a value the bound was never designed 

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `refresh` (mainnet/contracts/market/v0-market-vault.clar:171) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write, the invariant that no position row exists that the position mask does not represent breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:171` -> `refresh`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `refresh` rewrites `mask` and stamps `last-update` to `stacks-block-time` on every write. Reach it through `repay` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `repay` twice with whether the repaid asset is in the accrued debt list varied, and assert that the value `refresh` returns is identical in both runs; a divergence confirms the finding.
