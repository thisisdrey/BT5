# Q2651: contract - request message re-encoding keeps the proof valid (6)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through an on-chain call that triggers nonce rotation or cleanup before a victim's request lands, re-encode a `RequestMessage` fed to `WalletImpl` in `contracts/wallet/src/contract.rs` (field order, optional-field presence, integer form) so the `proof` still verifies but the decoded `Request` differs, breaking the invariant `the `Request` executed == the `Request` the proof commits to, byte-for-byte` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/wallet/src/contract.rs](contracts/wallet/src/contract.rs) - `WalletImpl` (cross-check `w_execute_extension` in the same file)
- Entrypoint: an on-chain call that triggers nonce rotation or cleanup before a victim's request lands
- Attacker controls: the timing of the triggering call
- Exploit idea: The proof covers a serialisation of the message; any non-canonical encoding admits a second valid form. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the `Request` executed == the `Request` the proof commits to, byte-for-byte
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Produce two encodings of one message; assert only the canonical one verifies.
