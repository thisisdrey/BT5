# Q4143: oracle-timestamp-fresh via supply-collateral-add: make two code sites that must agree disagree by an attacke

## Question
`oracle-timestamp-fresh` (mainnet/contracts/market/v0-4-market.clar:365) sets `delta` to u0 whenever `ts` exceeds `stacks-block-time`, so a future timestamp is maximally fresh, then requires `(>= ts prev)`. Can an unprivileged caller of `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), by choosing `min-shares` (the only slippage bound on the deposit leg), use that to make two code sites that must agree disagree by an attacker-chosen amount, violating the invariant that a value cached within a block still describes the state it was derived from and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:365` -> `oracle-timestamp-fresh`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `min-shares` (the only slippage bound on the deposit leg)
- Exploit idea: `oracle-timestamp-fresh` sets `delta` to u0 whenever `ts` exceeds `stacks-block-time`, so a future timestamp is maximally fresh, then requires `(>= ts prev)`. Reach it through `supply-collateral-add` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `oracle-timestamp-fresh` touches, run `supply-collateral-add` with `min-shares` (the only slippage bound on the deposit leg), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
