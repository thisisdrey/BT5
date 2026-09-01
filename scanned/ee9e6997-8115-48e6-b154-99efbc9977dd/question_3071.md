# Q3071: seq - Cow/borrow round-trip alters the value (7)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments, exploit a borrow/owned asymmetry in `Reversed` of `crates/serde-utils/src/seq.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/serde-utils/src/seq.rs](crates/serde-utils/src/seq.rs) - `Reversed` (cross-check `serialize_as` in the same file)
- Entrypoint: `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments
- Attacker controls: `token_ids`, `memo`, `msg` and every numeric field supplied as a string
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the same key appears twice in the encoded map.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `Reversed`; assert event and state agree.
