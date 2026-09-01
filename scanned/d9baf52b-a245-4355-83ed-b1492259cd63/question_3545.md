# Q3545: state - nonce window rotation permits replay (7)

## Question
Given the request is replayed against a sibling subwallet deployment, can an unprivileged attacker, entering through replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet, replay a `RequestMessage` through `DEFAULT_SUBWALLET_ID` in `contracts/wallet/src/state.rs` across the `old`/`current` bitmap rotation driven by `timeout` and `last_cleaned_at`, breaking the invariant `the number of times one signed `RequestMessage` executes == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/wallet/src/state.rs](contracts/wallet/src/state.rs) - `DEFAULT_SUBWALLET_ID` (cross-check `subwallet_id` in the same file)
- Entrypoint: replay of a `RequestMessage` observed on-chain, against the same or a sibling wallet
- Attacker controls: when it is replayed and which wallet instance it is sent to
- Exploit idea: A nonce recorded in `current` moves to `old` and is eventually dropped; if the message's validity window outlives the bitmap retention, the same signed request executes twice. Set-up: the request is replayed against a sibling subwallet deployment.
- Invariant to test: the number of times one signed `RequestMessage` executes == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Advance the sandbox clock past `2 * timeout` and resubmit a still-valid signed request; assert rejection.
