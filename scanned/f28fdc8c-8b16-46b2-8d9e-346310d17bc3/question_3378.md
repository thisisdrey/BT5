# Q3378: increment via supply-collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling `amount`, can an unprivileged attacker make `increment` (mainnet/contracts/market/v0-market-vault.clar:137) turn an accounting residue into a permanently unclosable position? `increment` advances the user-id nonce, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `increment` advances the user-id nonce. Reach it through `supply-collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `supply-collateral-add` in simnet and assert `increment` never returns a value that breaks the invariant.
