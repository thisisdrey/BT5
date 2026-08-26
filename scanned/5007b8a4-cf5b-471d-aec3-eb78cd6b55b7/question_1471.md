# Q1471: account id parsing and normalisation differences — upgrade_schedule.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, ids with uppercase characters, adjacent separators, maximum length, and trailing dots, with trailing bytes appended after a valid encoding, reach `protocol_version_to_vote_for_at_date` in `core/primitives/src/upgrade_schedule.rs` and make one component accept an id another rejects, splitting execution, breaking the invariant that account id validation is a single canonical function used by every caller, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/upgrade_schedule.rs` :: `protocol_version_to_vote_for_at_date`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: ids with uppercase characters, adjacent separators, maximum length, and trailing dots; with trailing bytes appended after a valid encoding
- Exploit idea: make one component accept an id another rejects, splitting execution
- Invariant to test: account id validation is a single canonical function used by every caller
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over id edge cases across all validation entry points
