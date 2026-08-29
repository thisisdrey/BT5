# Q3216: receive-underlying via redeem: reach a state the guard immediately upstream of it never c

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls the vault's available liquidity relative to the redemption reach `receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it pulls the underlying from a named account, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the vault's available liquidity relative to the redemption across its boundary values through `redeem` in simnet and assert `receive-underlying` never returns a value that breaks the invariant.
