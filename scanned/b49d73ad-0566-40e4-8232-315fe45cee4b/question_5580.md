# Q5580: get-cached-indexes via deposit: make a health check read a different position than the one

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls whether the vault is at a zero-supply or zero-asset edge reach `get-cached-indexes` (mainnet/contracts/market/v0-4-market.clar:944) in a state where it make a health check read a different position than the one that will exist? Given that it reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:944` -> `get-cached-indexes`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: whether the vault is at a zero-supply or zero-asset edge
- Exploit idea: `get-cached-indexes` reads the per-block cache entry that `resolve-ztoken` and the debt conversions depend on. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz whether the vault is at a zero-supply or zero-asset edge across its boundary values through `deposit` in simnet and assert `get-cached-indexes` never returns a value that breaks the invariant.
