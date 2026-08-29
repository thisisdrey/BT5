# Q3648: send-underlying via transfer: reach a state the guard immediately upstream of it never c

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls the timing relative to a pledge or a liquidation reach `send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it pushes the underlying under an `as-contract?` post-condition scope, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `transfer` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the timing relative to a pledge or a liquidation across its boundary values through `transfer` in simnet and assert `send-underlying` never returns a value that breaks the invariant.
