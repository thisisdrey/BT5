# Q5320: function call args at the argument-size limit — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through a `FunctionCall` action into an attacker-deployed contract, args whose length is exactly max_arguments_length, with a method name at the length limit, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `check_and_update_nonce` in `runtime/runtime/src/global_contracts.rs` and cross a boundary where the length check and the copy disagree, breaking the invariant that argument length checks bound every subsequent copy of the same buffer, leading to Critical - Network not being able to confirm new transactions (total network shutdown)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `check_and_update_nonce`
- Entrypoint: a `FunctionCall` action into an attacker-deployed contract
- Attacker controls: args whose length is exactly max_arguments_length, with a method name at the length limit; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: cross a boundary where the length check and the copy disagree
- Invariant to test: argument length checks bound every subsequent copy of the same buffer
- Expected Immunefi impact: Critical - Network not being able to confirm new transactions (total network shutdown)
- Fast validation: unit test at the exact argument-length boundary
