# Q5661: FunctionCall key method_names bypass — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through `AddKey` / `DeleteKey` actions on an attacker-owned account, method_names containing an empty string, a name with a trailing NUL or non-UTF8 byte, duplicates, and a list at the size limit, when the same input is submitted through two RPC nodes in the same block height, and additionally when the action is the first in a maximally long batched action list, reach `append_action_delete_key` in `runtime/runtime/src/receipt_manager.rs` and make the permitted-method comparison accept a method the key was never granted, breaking the invariant that a FunctionCall key can only invoke methods byte-identical to an entry in method_names, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_delete_key`
- Entrypoint: `AddKey` / `DeleteKey` actions on an attacker-owned account
- Attacker controls: method_names containing an empty string, a name with a trailing NUL or non-UTF8 byte, duplicates, and a list at the size limit; when the same input is submitted through two RPC nodes in the same block height; when the action is the first in a maximally long batched action list
- Exploit idea: make the permitted-method comparison accept a method the key was never granted
- Invariant to test: a FunctionCall key can only invoke methods byte-identical to an entry in method_names
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over method_names edge cases asserting MethodNameMismatch
