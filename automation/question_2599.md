# Q2599: mod - HashMap iteration order changes the settlement (4)

## Question
Given the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer, can an unprivileged attacker, entering through `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it, exploit that `execute_signed_intents` in `contracts/defuse/core/src/engine/mod.rs` iterates a `HashMap` whose order is not fixed, so the sender/receiver pairing produced by `finalize_into` (and therefore the emitted `mt_transfer` attribution) depends on ordering the attacker can influence via chosen account ids, breaking the invariant `the set of `Transfers` produced for a given delta set == a deterministic function of that delta set` and leading to temporary freezing of user funds?

## Target
- File/function: [contracts/defuse/core/src/engine/mod.rs](contracts/defuse/core/src/engine/mod.rs) - `execute_signed_intents` (cross-check `execute_signed_intent` in the same file)
- Entrypoint: `simulate_intents` to find a batch that reports balanced, then `execute_intents` to commit it
- Attacker controls: the entire batch across both calls
- Exploit idea: Deposits and withdrawals are sorted by amount with `sort_unstable_by_key(Reverse(amount))`; equal amounts leave ties broken by iteration order. Set-up: the batch mixes NEP-141, NEP-245 and NEP-171 legs under one signer.
- Invariant to test: the set of `Transfers` produced for a given delta set == a deterministic function of that delta set
- Expected Immunefi impact: High - Temporary freezing of user funds
- Fast validation: Run `finalize()` repeatedly on an equal-amount delta set across account-id permutations; assert identical `Transfers`.
