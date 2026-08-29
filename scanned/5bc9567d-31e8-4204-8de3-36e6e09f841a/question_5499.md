# Q5499: convert-to-shares-preview via accrue: reach a state the guard immediately upstream of it never c

## Question
`convert-to-shares-preview` (mainnet/contracts/vault/v0-vault-stx.clar:308) returns u0 outright when `total-assets-preview` is non-zero and supply is zero, minting nothing for a real deposit. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing the utilization the rate is interpolated at, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:308` -> `convert-to-shares-preview`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the utilization the rate is interpolated at
- Exploit idea: `convert-to-shares-preview` returns u0 outright when `total-assets-preview` is non-zero and supply is zero, minting nothing for a real deposit. Reach it through `accrue` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `convert-to-shares-preview` touches, run `accrue` with the utilization the rate is interpolated at, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
