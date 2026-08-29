# Q2538: relevant via collateral-remove: compose two individually correct mechanisms into an incorr

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling `receiver`, including a contract principal, can an unprivileged attacker make `relevant` (mainnet/contracts/market/v0-market-vault.clar:175) compose two individually correct mechanisms into an incorrect result? `relevant` drops any position row whose bit is not present in the enabled mask, so the invariant that no position row exists that the position mask does not represent would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:175` -> `relevant`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `relevant` drops any position row whose bit is not present in the enabled mask. Reach it through `collateral-remove` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `collateral-remove` in simnet and assert `relevant` never returns a value that breaks the invariant.
