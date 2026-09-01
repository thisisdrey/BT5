# Q2436: lib - request message re-encoding keeps the proof valid (8)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, re-encode a `RequestMessage` fed to `verify_request_msg` in `contracts/wallet/signatures/webauthn/src/lib.rs` (field order, optional-field presence, integer form) so the `proof` still verifies but the decoded `Request` differs, breaking the invariant `the `Request` executed == the `Request` the proof commits to, byte-for-byte` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/signatures/webauthn/src/lib.rs](contracts/wallet/signatures/webauthn/src/lib.rs) - `verify_request_msg` (cross-check `verify_offchain_msg` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: The proof covers a serialisation of the message; any non-canonical encoding admits a second valid form. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `Request` executed == the `Request` the proof commits to, byte-for-byte
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Produce two encodings of one message; assert only the canonical one verifies.
