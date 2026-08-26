# Q0939: FunctionCall key receiver_id binding — errors.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a receiver_id that differs from the key's receiver_id only by a normalisation-sensitive form or trailing separator, with the boundary value chosen exactly at the enforced limit, reach `metrics_label` in `core/primitives/src/errors.rs` and have the authorisation check and the receipt routing disagree about the receiver account, breaking the invariant that the receiver a FunctionCall key authorises is exactly the receiver the receipt is delivered to, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives/src/errors.rs` :: `metrics_label`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a receiver_id that differs from the key's receiver_id only by a normalisation-sensitive form or trailing separator; with the boundary value chosen exactly at the enforced limit
- Exploit idea: have the authorisation check and the receipt routing disagree about the receiver account
- Invariant to test: the receiver a FunctionCall key authorises is exactly the receiver the receipt is delivered to
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test asserting receiver comparison is raw-byte equality
