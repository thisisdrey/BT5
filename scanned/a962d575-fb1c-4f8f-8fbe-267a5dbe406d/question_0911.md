# Q0911: send-underlying via deposit: compose two individually correct mechanisms into an incorr

## Question
`send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) pushes the underlying under an `as-contract?` post-condition scope. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `amount`, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `deposit` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `deposit` call, then the attacker-shaped one with `amount`, and assert the attacker's net token balance change is zero or negative.
