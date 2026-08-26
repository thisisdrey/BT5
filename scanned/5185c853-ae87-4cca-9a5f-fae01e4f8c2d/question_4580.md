# Q4580: FunctionCall key receiver_id binding — gas.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a receiver_id that differs from the key's receiver_id only by a normalisation-sensitive form or trailing separator, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `json_schema` in `core/primitives-core/src/gas.rs` and have the authorisation check and the receipt routing disagree about the receiver account, breaking the invariant that the receiver a FunctionCall key authorises is exactly the receiver the receipt is delivered to, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives-core/src/gas.rs` :: `json_schema`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a receiver_id that differs from the key's receiver_id only by a normalisation-sensitive form or trailing separator; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: have the authorisation check and the receipt routing disagree about the receiver account
- Invariant to test: the receiver a FunctionCall key authorises is exactly the receiver the receipt is delivered to
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test asserting receiver comparison is raw-byte equality
