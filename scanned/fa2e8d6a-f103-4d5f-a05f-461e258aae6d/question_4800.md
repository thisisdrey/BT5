# Q4800: account id parsing and normalisation differences — serialize.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, ids with uppercase characters, adjacent separators, maximum length, and trailing dots, with a non-minimal length prefix, and additionally with a duplicate or out-of-range enum discriminant, reach `base64_schema` in `core/primitives-core/src/serialize.rs` and make one component accept an id another rejects, splitting execution, breaking the invariant that account id validation is a single canonical function used by every caller, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/serialize.rs` :: `base64_schema`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: ids with uppercase characters, adjacent separators, maximum length, and trailing dots; with a non-minimal length prefix; with a duplicate or out-of-range enum discriminant
- Exploit idea: make one component accept an id another rejects, splitting execution
- Invariant to test: account id validation is a single canonical function used by every caller
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over id edge cases across all validation entry points
