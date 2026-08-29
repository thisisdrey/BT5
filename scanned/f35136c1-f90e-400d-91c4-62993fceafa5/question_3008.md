# Q3008: is-healthy-with-mask via supply-collateral-add: reach a state the guard immediately upstream of it never c

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls `amount` reach `is-healthy-with-mask` (mainnet/contracts/market/v0-4-market.clar:663) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it resolves an egroup for a caller-influenced mask and applies its LTV-BORROW, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:663` -> `is-healthy-with-mask`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW. Reach it through `supply-collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with `amount` varied, and assert that the value `is-healthy-with-mask` returns is identical in both runs; a divergence confirms the finding.
