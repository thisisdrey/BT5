# Q1490: cleanup - Cow/borrow round-trip alters the value (4)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, exploit a borrow/owned asymmetry in `DefaultVacantEntry` of `crates/map-utils/src/cleanup.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/map-utils/src/cleanup.rs](crates/map-utils/src/cleanup.rs) - `DefaultVacantEntry` (cross-check `DefaultEntry` in the same file)
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `DefaultVacantEntry`; assert event and state agree.
