# Q3600: accrue-user-collateral via accrue: reach a state the guard immediately upstream of it never c

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls whether an earlier call in the same block already advanced last-update reach `accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it accrues only rows that `is-ztoken` recognises, skipping everything else, the invariant that only the acting principal's own position is mutated breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `accrue` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz whether an earlier call in the same block already advanced last-update across its boundary values through `accrue` in simnet and assert `accrue-user-collateral` never returns a value that breaks the invariant.
