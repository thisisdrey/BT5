# Q3560: accrue-user-collateral via liquidate: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls which collateral and debt asset pair is targeted reach `accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it accrues only rows that `is-ztoken` recognises, skipping everything else, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `liquidate` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with which collateral and debt asset pair is targeted varied, and assert that the value `accrue-user-collateral` returns is identical in both runs; a divergence confirms the finding.
