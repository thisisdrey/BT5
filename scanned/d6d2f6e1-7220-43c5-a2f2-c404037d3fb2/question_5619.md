# Q5619: calc-cumulative-debt via accrue: reach a state the guard immediately upstream of it never c

## Question
`calc-cumulative-debt` (mainnet/contracts/vault/v0-vault-stx.clar:180) multiplies scaled principal by an index. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing the utilization the rate is interpolated at, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:180` -> `calc-cumulative-debt`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the utilization the rate is interpolated at
- Exploit idea: `calc-cumulative-debt` multiplies scaled principal by an index. Reach it through `accrue` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `calc-cumulative-debt` touches, run `accrue` with the utilization the rate is interpolated at, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
