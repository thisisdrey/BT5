# Q5779: multi - signature malleability admitting a second valid encoding (6)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, submit a second, differently-encoded signature over the same message that `verify` in `contracts/defuse/core/src/payload/multi.rs` still accepts, so one authorisation yields two distinct `MultiPayload` values with two distinct `hash()` values, breaking the invariant `the number of distinct accepted `MultiPayload` encodings per signed message == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/payload/multi.rs](contracts/defuse/core/src/payload/multi.rs) - `verify` (cross-check `MultiPayload` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Use low-S/high-S, a non-canonical scalar, a differing recovery id, or trailing/leading encoding slack that the verifier normalises but the hash does not. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the number of distinct accepted `MultiPayload` encodings per signed message == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Unit-test `verify` with both encodings; assert both return `Some(pk)` and produce different `hash()` outputs.
