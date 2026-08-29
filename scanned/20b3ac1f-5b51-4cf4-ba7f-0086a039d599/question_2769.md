# Q2769: increment via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling whether this asset is already collateral (the is-new-collateral branch), drive `increment` (mainnet/contracts/market/v0-market-vault.clar:137) — which advances the user-id nonce — to convert a rounding direction into a repeatable extraction, breaking the invariant that conversions never round in the user's favour in either direction, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `increment` advances the user-id nonce. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `increment` touches, run `collateral-add` with whether this asset is already collateral (the is-new-collateral branch), recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
