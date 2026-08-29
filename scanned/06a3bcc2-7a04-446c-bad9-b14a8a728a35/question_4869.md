# Q4869: total-supply-preview via deposit: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), controlling `recipient`, including a contract principal, drive `total-supply-preview` (mainnet/contracts/vault/v0-vault-stx.clar:363) — which adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that only the acting principal's own position is mutated, and cause temporary freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:363` -> `total-supply-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `total-supply-preview` adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against. Reach it through `deposit` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Snapshot every state variable `total-supply-preview` touches, run `deposit` with `recipient`, including a contract principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
