# Q3528: calc-index-next via accrue: reach a state the guard immediately upstream of it never c

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls whether an earlier call in the same block already advanced last-update reach `calc-index-next` (mainnet/contracts/vault/v0-vault-stx.clar:183) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it applies a multiplier to the current index, the invariant that only the acting principal's own position is mutated breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:183` -> `calc-index-next`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `calc-index-next` applies a multiplier to the current index. Reach it through `accrue` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz whether an earlier call in the same block already advanced last-update across its boundary values through `accrue` in simnet and assert `calc-index-next` never returns a value that breaks the invariant.
