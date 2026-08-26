# Q5304: access key deletion racing an in-flight receipt — mod.rs

## Question
Can an unprivileged mainnet account, entering through `AddKey` / `DeleteKey` actions on an attacker-owned account, a DeleteKey in the same block as a receipt already authorised by that key, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `is_delegate` in `core/primitives/src/action/mod.rs` and have the in-flight receipt still execute with permissions the account has revoked, breaking the invariant that authorisation is re-evaluated against state at execution, not only at signing, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/action/mod.rs` :: `is_delegate`
- Entrypoint: `AddKey` / `DeleteKey` actions on an attacker-owned account
- Attacker controls: a DeleteKey in the same block as a receipt already authorised by that key; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: have the in-flight receipt still execute with permissions the account has revoked
- Invariant to test: authorisation is re-evaluated against state at execution, not only at signing
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test interleaving DeleteKey and the authorised receipt
