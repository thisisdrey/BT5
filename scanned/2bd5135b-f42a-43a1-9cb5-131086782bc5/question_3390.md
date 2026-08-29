# Q3390: vault-accrue via deposit: turn an accounting residue into a permanently unclosable p

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `recipient`, including a contract principal, can an unprivileged attacker make `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) turn an accounting residue into a permanently unclosable position? `vault-accrue` dispatches accrual to one of six vaults by asset id, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `recipient`, including a contract principal across its boundary values through `deposit` in simnet and assert `vault-accrue` never returns a value that breaks the invariant.
