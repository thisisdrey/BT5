# Q2970: get-egroup via collateral-remove: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) while controlling the `ft` trait principal, can an unprivileged attacker make `get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) turn an accounting residue into a permanently unclosable position? `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `collateral-remove` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `collateral-remove` in simnet and assert `get-egroup` never returns a value that breaks the invariant.
