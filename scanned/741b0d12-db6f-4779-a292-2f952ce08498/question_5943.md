# Q5943: interest-rate via accrue: convert a rounding direction into a repeatable extraction

## Question
`interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) interpolates the packed curve at the current utilization. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing whether an earlier call in the same block already advanced last-update, use that to convert a rounding direction into a repeatable extraction, violating the invariant that only the acting principal's own position is mutated and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `accrue` and convert a rounding direction into a repeatable extraction.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `interest-rate` touches, run `accrue` with whether an earlier call in the same block already advanced last-update, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
