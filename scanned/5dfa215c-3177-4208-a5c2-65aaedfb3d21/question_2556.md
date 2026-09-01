# Q2556: lib - request message re-encoding keeps the proof valid (10)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, re-encode a `RequestMessage` fed to `NoSign` in `contracts/wallet/signatures/no-sign/src/lib.rs` (field order, optional-field presence, integer form) so the `proof` still verifies but the decoded `Request` differs, breaking the invariant `the `Request` executed == the `Request` the proof commits to, byte-for-byte` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/signatures/no-sign/src/lib.rs](contracts/wallet/signatures/no-sign/src/lib.rs) - `NoSign` (cross-check `verify_offchain_msg` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: The proof covers a serialisation of the message; any non-canonical encoding admits a second valid form. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `Request` executed == the `Request` the proof commits to, byte-for-byte
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Produce two encodings of one message; assert only the canonical one verifies.
