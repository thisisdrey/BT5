# Q1537: state - metadata or decimals change alters the value of custodied balances (5)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, reach `with_code_hash` in `contracts/outlayer/app/src/state.rs` so a token's decimals/metadata change after the Verifier has credited balances denominated under the old value, breaking the invariant `the denomination of a credited balance == the denomination at redemption time` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/outlayer/app/src/state.rs](contracts/outlayer/app/src/state.rs) - `with_code_hash` (cross-check `with_code_url` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: Probe whether the mutation is reachable without the owner role and whether any consumer caches the old value. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the denomination of a credited balance == the denomination at redemption time
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Change metadata after a deposit; assert redemption uses consistent denomination.
