# Q1203: principal-ratio-reduction via deposit: turn an accounting residue into a permanently unclosable p

## Question
`principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:406) derives a principal reduction from an amount, the scaled principal and the previewed debt. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing the vault's supply and asset state at the moment of the call, use that to turn an accounting residue into a permanently unclosable position, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:406` -> `principal-ratio-reduction`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `principal-ratio-reduction` derives a principal reduction from an amount, the scaled principal and the previewed debt. Reach it through `deposit` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `principal-ratio-reduction` touches, run `deposit` with the vault's supply and asset state at the moment of the call, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
