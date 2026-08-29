# Q2178: write-feed via borrow: compose two individually correct mechanisms into an incorr

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling `receiver`, including a contract principal, can an unprivileged attacker make `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) compose two individually correct mechanisms into an incorrect result? `write-feed` applies one Pyth price-feed update and folds its status, so the invariant that no position row exists that the position mask does not represent would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `borrow` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `receiver`, including a contract principal across its boundary values through `borrow` in simnet and assert `write-feed` never returns a value that breaks the invariant.
