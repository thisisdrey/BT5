# Q0169: next-liquidity-index via accrue: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), controlling the block time at which accrual is first triggered in a block, drive `next-liquidity-index` (mainnet/contracts/vault/v0-vault-stx.clar:392) — which rounds the liquidity multiplier down while `next-index` rounds the debt multiplier up over the same interval — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:392` -> `next-liquidity-index`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `next-liquidity-index` rounds the liquidity multiplier down while `next-index` rounds the debt multiplier up over the same interval. Reach it through `accrue` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `accrue` with the block time at which accrual is first triggered in a block, then read `next-liquidity-index` state before and after in the same block and assert the two sides of the invariant are equal.
