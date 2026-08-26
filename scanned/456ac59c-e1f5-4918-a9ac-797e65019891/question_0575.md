# Q0575: FunctionCall key allowance vs attached deposit — verifier.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a FunctionCall access key whose allowance is smaller than the fee of the action list it authorises, with the boundary value chosen exactly at the enforced limit, reach `verify_and_charge_gas_key_tx_ephemeral` in `runtime/runtime/src/verifier.rs` and get the deposit/fee charged against the account while the allowance check reads a different, smaller quantity, breaking the invariant that the allowance charged equals the total balance the key actually moves, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/verifier.rs` :: `verify_and_charge_gas_key_tx_ephemeral`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a FunctionCall access key whose allowance is smaller than the fee of the action list it authorises; with the boundary value chosen exactly at the enforced limit
- Exploit idea: get the deposit/fee charged against the account while the allowance check reads a different, smaller quantity
- Invariant to test: the allowance charged equals the total balance the key actually moves
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test comparing allowance delta against account balance delta for one call
