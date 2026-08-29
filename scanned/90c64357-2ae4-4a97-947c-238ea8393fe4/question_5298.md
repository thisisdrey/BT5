# Q5298: lookup via borrow: make an aggregate and its per-item breakdown disagree

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling the future mask produced by the new debt bit, can an unprivileged attacker make `lookup` (mainnet/contracts/registry/v0-assets.clar:139) make an aggregate and its per-item breakdown disagree? `lookup` returns the registry record, including the `decimals` captured once at registration, so the invariant that only the acting principal's own position is mutated would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:139` -> `lookup`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the future mask produced by the new debt bit
- Exploit idea: `lookup` returns the registry record, including the `decimals` captured once at registration. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the future mask produced by the new debt bit across its boundary values through `borrow` in simnet and assert `lookup` never returns a value that breaks the invariant.
