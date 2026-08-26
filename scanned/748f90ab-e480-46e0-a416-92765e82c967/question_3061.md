# Q3061: FunctionCall key method_names bypass — action_validation.rs

## Question
Can an unprivileged mainnet account, entering through `AddKey` / `DeleteKey` actions on an attacker-owned account, method_names containing an empty string, a name with a trailing NUL or non-UTF8 byte, duplicates, and a list at the size limit, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `validate_access_key_permission` in `runtime/runtime/src/action_validation.rs` and make the permitted-method comparison accept a method the key was never granted, breaking the invariant that a FunctionCall key can only invoke methods byte-identical to an entry in method_names, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/action_validation.rs` :: `validate_access_key_permission`
- Entrypoint: `AddKey` / `DeleteKey` actions on an attacker-owned account
- Attacker controls: method_names containing an empty string, a name with a trailing NUL or non-UTF8 byte, duplicates, and a list at the size limit; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: make the permitted-method comparison accept a method the key was never granted
- Invariant to test: a FunctionCall key can only invoke methods byte-identical to an entry in method_names
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: unit test over method_names edge cases asserting MethodNameMismatch
