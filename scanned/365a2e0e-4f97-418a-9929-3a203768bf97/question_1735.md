# Q1735: lib - metadata or decimals change alters the value of custodied balances (8)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a token the attacker issued and then had the Verifier custody, reach `withdraw_to` in `contracts/poa/token/src/lib.rs` so a token's decimals/metadata change after the Verifier has credited balances denominated under the old value, breaking the invariant `the denomination of a credited balance == the denomination at redemption time` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/poa/token/src/lib.rs](contracts/poa/token/src/lib.rs) - `withdraw_to` (cross-check `WITHDRAW_MEMO_PREFIX` in the same file)
- Entrypoint: a token the attacker issued and then had the Verifier custody
- Attacker controls: the token's behaviour on transfer, refund and metadata reads
- Exploit idea: Probe whether the mutation is reachable without the owner role and whether any consumer caches the old value. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the denomination of a credited balance == the denomination at redemption time
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Change metadata after a deposit; assert redemption uses consistent denomination.
