# Q2408: mod - request message re-encoding keeps the proof valid (2)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, re-encode a `RequestMessage` fed to `PER_INTERNAL_OP` in `contracts/wallet/src/request/mod.rs` (field order, optional-field presence, integer form) so the `proof` still verifies but the decoded `Request` differs, breaking the invariant `the `Request` executed == the `Request` the proof commits to, byte-for-byte` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/request/mod.rs](contracts/wallet/src/request/mod.rs) - `PER_INTERNAL_OP` (cross-check `is_empty` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: The proof covers a serialisation of the message; any non-canonical encoding admits a second valid form. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `Request` executed == the `Request` the proof commits to, byte-for-byte
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Produce two encodings of one message; assert only the canonical one verifies.
