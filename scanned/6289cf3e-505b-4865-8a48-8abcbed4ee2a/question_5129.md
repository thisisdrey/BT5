# Q5129: principal-ratio-reduction via accrue: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), controlling the block time at which accrual is first triggered in a block, drive `principal-ratio-reduction` (mainnet/contracts/vault/v0-vault-stx.clar:406) — which derives a principal reduction from an amount, the scaled principal and the previewed debt — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:406` -> `principal-ratio-reduction`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `principal-ratio-reduction` derives a principal reduction from an amount, the scaled principal and the previewed debt. Reach it through `accrue` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `accrue` call, then the attacker-shaped one with the block time at which accrual is first triggered in a block, and assert the attacker's net token balance change is zero or negative.
