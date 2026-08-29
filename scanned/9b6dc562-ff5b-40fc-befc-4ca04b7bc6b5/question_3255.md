# Q3255: calc-cumulative-debt via deposit: make an aggregate and its per-item breakdown disagree

## Question
`calc-cumulative-debt` (mainnet/contracts/vault/v0-vault-stx.clar:180) multiplies scaled principal by an index. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing `recipient`, including a contract principal, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:180` -> `calc-cumulative-debt`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `calc-cumulative-debt` multiplies scaled principal by an index. Reach it through `deposit` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `calc-cumulative-debt` touches, run `deposit` with `recipient`, including a contract principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
