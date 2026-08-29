# Q4439: calc-multiplier-delta via accrue: make two code sites that must agree disagree by an attacke

## Question
`calc-multiplier-delta` (mainnet/contracts/vault/v0-vault-stx.clar:170) compounds a rate over `time-delta` with a caller-independent rounding flag. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing whether an earlier call in the same block already advanced last-update, use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:170` -> `calc-multiplier-delta`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `calc-multiplier-delta` compounds a rate over `time-delta` with a caller-independent rounding flag. Reach it through `accrue` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `accrue` call, then the attacker-shaped one with whether an earlier call in the same block already advanced last-update, and assert the attacker's net token balance change is zero or negative.
