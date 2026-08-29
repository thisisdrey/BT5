# Q1884: relevant via repay: make an aggregate and its per-item breakdown disagree

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `relevant` (mainnet/contracts/market/v0-market-vault.clar:175) in a state where it make an aggregate and its per-item breakdown disagree? Given that it drops any position row whose bit is not present in the enabled mask, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:175` -> `relevant`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `relevant` drops any position row whose bit is not present in the enabled mask. Reach it through `repay` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount`, including far above the real debt (the capping path) across its boundary values through `repay` in simnet and assert `relevant` never returns a value that breaks the invariant.
