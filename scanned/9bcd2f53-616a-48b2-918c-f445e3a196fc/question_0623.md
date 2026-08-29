# Q0623: accrue-user-collateral via liquidate: compose two individually correct mechanisms into an incorr

## Question
`accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) accrues only rows that `is-ztoken` recognises, skipping everything else. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing which collateral and debt asset pair is targeted, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `liquidate` call, then the attacker-shaped one with which collateral and debt asset pair is targeted, and assert the attacker's net token balance change is zero or negative.
