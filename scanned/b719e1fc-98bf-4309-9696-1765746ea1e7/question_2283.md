# Q2283: unpack-u16 via liquidate-multi: satisfy a bound with a value the bound was never designed 

## Question
`unpack-u16` (mainnet/contracts/vault/v0-vault-stx.clar:259) unpacks eight u16 curve fields from one packed word. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the full batch list and its ordering, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:259` -> `unpack-u16`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `unpack-u16` unpacks eight u16 curve fields from one packed word. Reach it through `liquidate-multi` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `unpack-u16` touches, run `liquidate-multi` with the full batch list and its ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
