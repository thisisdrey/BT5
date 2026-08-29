# Q3336: debt-preview via redeem: reach a state the guard immediately upstream of it never c

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls the vault's available liquidity relative to the redemption reach `debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it computes cumulative debt from `principal-scaled` and the FORWARD index, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the vault's available liquidity relative to the redemption across its boundary values through `redeem` in simnet and assert `debt-preview` never returns a value that breaks the invariant.
