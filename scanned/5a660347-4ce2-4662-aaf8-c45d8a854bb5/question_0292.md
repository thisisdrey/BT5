# Q0292: state - nonce window rotation permits replay (2)

## Question
Given the wallet's nonce bitmap has just rotated `current` into `old`, can an unprivileged attacker, entering through `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit, replay a `RequestMessage` through `extensions` in `contracts/wallet/src/state.rs` across the `old`/`current` bitmap rotation driven by `timeout` and `last_cleaned_at`, breaking the invariant `the number of times one signed `RequestMessage` executes == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/state.rs](contracts/wallet/src/state.rs) - `extensions` (cross-check `subwallet_id` in the same file)
- Entrypoint: `w_execute_extension(request: Request)` called from any predecessor with a non-zero deposit
- Attacker controls: the `Request` contents, the calling account id, and the attached deposit
- Exploit idea: A nonce recorded in `current` moves to `old` and is eventually dropped; if the message's validity window outlives the bitmap retention, the same signed request executes twice. Set-up: the wallet's nonce bitmap has just rotated `current` into `old`.
- Invariant to test: the number of times one signed `RequestMessage` executes == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Advance the sandbox clock past `2 * timeout` and resubmit a still-valid signed request; assert rejection.
