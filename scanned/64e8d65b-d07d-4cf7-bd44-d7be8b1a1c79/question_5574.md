# Q5574: nonces - nonce window rotation permits replay (14)

## Question
Given the wallet is built on the `no-sign` signature schema, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, replay a `RequestMessage` through `NonceError` in `contracts/wallet/src/nonces.rs` across the `old`/`current` bitmap rotation driven by `timeout` and `last_cleaned_at`, breaking the invariant `the number of times one signed `RequestMessage` executes == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/nonces.rs](contracts/wallet/src/nonces.rs) - `NonceError` (cross-check `last_cleaned_at` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: A nonce recorded in `current` moves to `old` and is eventually dropped; if the message's validity window outlives the bitmap retention, the same signed request executes twice. Set-up: the wallet is built on the `no-sign` signature schema.
- Invariant to test: the number of times one signed `RequestMessage` executes == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Advance the sandbox clock past `2 * timeout` and resubmit a still-valid signed request; assert rejection.
