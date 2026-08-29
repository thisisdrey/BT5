# Q5119: principal-ratio-reduction via deposit: reach a state the guard immediately upstream of it never c

## Question
`principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:406) derives a principal reduction from an amount, the scaled principal and the previewed debt. Can an unprivileged caller of `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), by choosing the vault's supply and asset state at the moment of the call, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:406` -> `principal-ratio-reduction`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: the vault's supply and asset state at the moment of the call
- Exploit idea: `principal-ratio-reduction` derives a principal reduction from an amount, the scaled principal and the previewed debt. Reach it through `deposit` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `deposit` with the vault's supply and asset state at the moment of the call, then read `principal-ratio-reduction` state before and after in the same block and assert the two sides of the invariant are equal.
