# Q3183: near - Cow/borrow round-trip alters the value (8)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, exploit a borrow/owned asymmetry in `contains_key` of `crates/map-utils/src/near.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/map-utils/src/near.rs](crates/map-utils/src/near.rs) - `contains_key` (cross-check `values` in the same file)
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the same key appears twice in the encoded map.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `contains_key`; assert event and state agree.
