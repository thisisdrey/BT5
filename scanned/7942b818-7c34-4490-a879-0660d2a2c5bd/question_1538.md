# Q1538: contract - metadata or decimals change alters the value of custodied balances (3)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, reach `POA_TOKEN_WASM` in `contracts/poa/factory/src/contract.rs` so a token's decimals/metadata change after the Verifier has credited balances denominated under the old value, breaking the invariant `the denomination of a credited balance == the denomination at redemption time` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/factory/src/contract.rs](contracts/poa/factory/src/contract.rs) - `POA_TOKEN_WASM` (cross-check `Contract` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: Probe whether the mutation is reachable without the owner role and whether any consumer caches the old value. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the denomination of a credited balance == the denomination at redemption time
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Change metadata after a deposit; assert redemption uses consistent denomination.
