# Q4981: resolve-pyth via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling call ordering within the block, drive `resolve-pyth` (mainnet/contracts/market/v0-4-market.clar:312) — which reads the Pyth storage record for a 32-byte ident — to turn an accounting residue into a permanently unclosable position, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:312` -> `resolve-pyth`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: call ordering within the block
- Exploit idea: `resolve-pyth` reads the Pyth storage record for a 32-byte ident. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with call ordering within the block, then read `resolve-pyth` state before and after in the same block and assert the two sides of the invariant are equal.
