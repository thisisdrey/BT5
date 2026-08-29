# Q3320: debt-preview via collateral-remove-redeem: reach a state the guard immediately upstream of it never c

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `receiver` for the underlying leg reach `debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it computes cumulative debt from `principal-scaled` and the FORWARD index, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `collateral-remove-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with `receiver` for the underlying leg varied, and assert that the value `debt-preview` returns is identical in both runs; a divergence confirms the finding.
