# Q3848: send-underlying via deposit: reach a state the guard immediately upstream of it never c

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `amount` reach `send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it pushes the underlying under an `as-contract?` post-condition scope, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `deposit` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with `amount` varied, and assert that the value `send-underlying` returns is identical in both runs; a divergence confirms the finding.
