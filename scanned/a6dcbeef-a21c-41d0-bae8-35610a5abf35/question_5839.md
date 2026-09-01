# Q5839: multi - signature malleability admitting a second valid encoding (7)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, submit a second, differently-encoded signature over the same message that `MultiPayload` in `contracts/defuse/core/src/payload/multi.rs` still accepts, so one authorisation yields two distinct `MultiPayload` values with two distinct `hash()` values, breaking the invariant `the number of distinct accepted `MultiPayload` encodings per signed message == 1` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/payload/multi.rs](contracts/defuse/core/src/payload/multi.rs) - `MultiPayload` (cross-check `verify` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Use low-S/high-S, a non-canonical scalar, a differing recovery id, or trailing/leading encoding slack that the verifier normalises but the hash does not. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the number of distinct accepted `MultiPayload` encodings per signed message == 1
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Unit-test `MultiPayload` with both encodings; assert both return `Some(pk)` and produce different `hash()` outputs.
