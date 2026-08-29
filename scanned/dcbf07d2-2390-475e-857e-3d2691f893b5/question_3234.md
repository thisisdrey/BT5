# Q3234: merge-price via collateral-remove: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling the `ft` trait principal, can an unprivileged attacker make `merge-price` (mainnet/contracts/market/v0-4-market.clar:506) turn an accounting residue into a permanently unclosable position? `merge-price` attaches a price to an asset record by position in the fold, not by asset id, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `collateral-remove` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `collateral-remove` in simnet and assert `merge-price` never returns a value that breaks the invariant.
