# Q5848: total-supply-preview via deposit: make a health check read a different position than the one

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `recipient`, including a contract principal reach `total-supply-preview` (mainnet/contracts/vault/v0-vault-stx.clar:363) in a state where it make a health check read a different position than the one that will exist? Given that it adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against, the invariant that conversions never round in the user's favour in either direction breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:363` -> `total-supply-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `total-supply-preview` adds the not-yet-minted `calc-treasury-lp-preview` to the live supply that both conversions price against. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `deposit` with `recipient`, including a contract principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
