# Q5933: message - request message re-encoding keeps the proof valid (8)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, re-encode a `RequestMessage` fed to `deadline` in `contracts/wallet/src/message.rs` (field order, optional-field presence, integer form) so the `proof` still verifies but the decoded `Request` differs, breaking the invariant `the `Request` executed == the `Request` the proof commits to, byte-for-byte` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/message.rs](contracts/wallet/src/message.rs) - `deadline` (cross-check `as_extension_of` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: The proof covers a serialisation of the message; any non-canonical encoding admits a second valid form. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: the `Request` executed == the `Request` the proof commits to, byte-for-byte
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Produce two encodings of one message; assert only the canonical one verifies.
