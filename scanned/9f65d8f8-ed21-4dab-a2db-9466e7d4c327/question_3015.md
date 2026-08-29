# Q3015: find-superset via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
`find-superset` (mainnet/contracts/registry/v0-egroup.clar:262) returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing the set of assets held, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:262` -> `find-superset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `find-superset` returns the FIRST mask that is a superset, walking buckets in population order rather than finding the tightest. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `find-superset` touches, run `collateral-remove` with the set of assets held, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
