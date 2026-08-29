# Q4043: calc-multiplier-delta via deposit: make two code sites that must agree disagree by an attacke

## Question
`calc-multiplier-delta` (mainnet/contracts/vault/v0-vault-stx.clar:170) compounds a rate over `time-delta` with a caller-independent rounding flag. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `min-out`, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that conversions never round in the user's favour in either direction and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:170` -> `calc-multiplier-delta`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `calc-multiplier-delta` compounds a rate over `time-delta` with a caller-independent rounding flag. Reach it through `deposit` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `deposit` call, then the attacker-shaped one with `min-out`, and assert the attacker's net token balance change is zero or negative.
