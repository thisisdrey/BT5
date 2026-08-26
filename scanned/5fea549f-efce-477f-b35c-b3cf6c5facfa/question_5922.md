# Q5922: account id parsing and normalisation differences — hash.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, ids with uppercase characters, adjacent separators, maximum length, and trailing dots, with a duplicate or out-of-range enum discriminant, and additionally with nesting at exactly the maximum accepted depth, reach `json_schema` in `core/primitives-core/src/hash.rs` and make one component accept an id another rejects, splitting execution, breaking the invariant that account id validation is a single canonical function used by every caller, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/hash.rs` :: `json_schema`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: ids with uppercase characters, adjacent separators, maximum length, and trailing dots; with a duplicate or out-of-range enum discriminant; with nesting at exactly the maximum accepted depth
- Exploit idea: make one component accept an id another rejects, splitting execution
- Invariant to test: account id validation is a single canonical function used by every caller
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over id edge cases across all validation entry points
