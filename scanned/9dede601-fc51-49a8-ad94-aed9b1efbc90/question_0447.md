# Q0447: active via borrow: compose two individually correct mechanisms into an incorr

## Question
`active` (mainnet/contracts/registry/v0-egroup.clar:238) lists candidate bucket masks at or above a population. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing the `ft` trait principal, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:238` -> `active`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `active` lists candidate bucket masks at or above a population. Reach it through `borrow` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `active` touches, run `borrow` with the `ft` trait principal, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
