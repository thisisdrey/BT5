# Q2637: price-multi-resolve via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling which borrowers are placed early versus late in the batch, drive `price-multi-resolve` (mainnet/contracts/market/v0-4-market.clar:397) — which folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end — to convert a rounding direction into a repeatable extraction, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:397` -> `price-multi-resolve`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `price-multi-resolve` touches, run `liquidate-multi` with which borrowers are placed early versus late in the batch, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
