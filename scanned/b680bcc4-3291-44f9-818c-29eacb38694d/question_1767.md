# Q1767: next-index via supply-collateral-add: turn an accounting residue into a permanently unclosable p

## Question
`next-index` (mainnet/contracts/vault/v0-vault-stx.clar:379) returns the stale `index` unchanged when the accrue pause state is set, instead of reverting. Can an unprivileged caller of `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), by choosing the `ft` trait principal deciding which vault is routed to, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:379` -> `next-index`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `next-index` returns the stale `index` unchanged when the accrue pause state is set, instead of reverting. Reach it through `supply-collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `next-index` touches, run `supply-collateral-add` with the `ft` trait principal deciding which vault is routed to, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
