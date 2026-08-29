# Q5781: find via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling `amount`, drive `find` (mainnet/contracts/registry/v0-assets.clar:135) — which resolves an asset record from a principal through the `reverse` map — to turn an accounting residue into a permanently unclosable position, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:135` -> `find`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `find` resolves an asset record from a principal through the `reverse` map. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `find` touches, run `collateral-add` with `amount`, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
