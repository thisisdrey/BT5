# Q0439: vault-system-repay via liquidate: compose two individually correct mechanisms into an incorr

## Question
`vault-system-repay` (mainnet/contracts/market/v0-4-market.clar:207) routes a repayment to one of six vaults by asset id. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing `borrower`, any third-party principal, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:207` -> `vault-system-repay`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `vault-system-repay` routes a repayment to one of six vaults by asset id. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate` with `borrower`, any third-party principal, then read `vault-system-repay` state before and after in the same block and assert the two sides of the invariant are equal.
