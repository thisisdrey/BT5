# Q5337: gas refund rounding on partially burnt gas — action_validation.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a call that burns a gas amount chosen to maximise the rounding residue at the current gas price, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `validate_transfer_to_gas_key_action` in `runtime/runtime/src/action_validation.rs` and make repeated refunds net the attacker more balance than was prepaid, breaking the invariant that prepaid balance equals burnt cost plus refunded balance for every receipt, exactly, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/action_validation.rs` :: `validate_transfer_to_gas_key_action`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a call that burns a gas amount chosen to maximise the rounding residue at the current gas price; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: make repeated refunds net the attacker more balance than was prepaid
- Invariant to test: prepaid balance equals burnt cost plus refunded balance for every receipt, exactly
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: property test over burnt-gas values asserting the balance identity holds
