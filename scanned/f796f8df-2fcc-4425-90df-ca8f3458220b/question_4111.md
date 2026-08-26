# Q4111: gas refund rounding on partially burnt gas — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, a call that burns a gas amount chosen to maximise the rounding residue at the current gas price, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `append_action_add_gas_key_with_full_access` in `runtime/runtime/src/receipt_manager.rs` and make repeated refunds net the attacker more balance than was prepaid, breaking the invariant that prepaid balance equals burnt cost plus refunded balance for every receipt, exactly, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_add_gas_key_with_full_access`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: a call that burns a gas amount chosen to maximise the rounding residue at the current gas price; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: make repeated refunds net the attacker more balance than was prepaid
- Invariant to test: prepaid balance equals burnt cost plus refunded balance for every receipt, exactly
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: property test over burnt-gas values asserting the balance identity holds
