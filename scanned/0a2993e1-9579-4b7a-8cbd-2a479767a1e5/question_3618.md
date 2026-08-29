# Q3618: subset via repay: turn an accounting residue into a permanently unclosable p

## Question
Entering through `repay` (mainnet/contracts/market/v0-4-market.clar:1316) while controlling `amount`, including far above the real debt (the capping path), can an unprivileged attacker make `subset` (mainnet/contracts/market/v0-market-vault.clar:100) turn an accounting residue into a permanently unclosable position? `subset` tests bitmask containment, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:100` -> `subset`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `subset` tests bitmask containment. Reach it through `repay` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz `amount`, including far above the real debt (the capping path) across its boundary values through `repay` in simnet and assert `subset` never returns a value that breaks the invariant.
