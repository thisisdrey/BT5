### Title
`remove_account` fails to purge Promise-Yield state, allowing a deleted-and-recreated account to inherit and execute a stale yield callback - (`core/store/src/utils/mod.rs`)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only clears `TrieKey::Account`, `TrieKey::ContractCode`, access/gas keys, and `TrieKey::ContractData` for a deleted account, but never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId`. Because `TrieKey::PromiseYieldTimeout` entries are stored in a global (non-account-scoped) queue and reference the account only by `account_id`/`data_id`, a timeout that outlives a `DeleteAccount`+`CreateAccount` cycle on the same `account_id` can resolve against the re-created account's new contract, executing orphaned callback state that no longer corresponds to any live yield created by the new deployment.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) explicitly removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- access keys / gas key nonces (via prefix iteration)
- `TrieKey::ContractData` (via prefix iteration)

It never calls `remove_promise_yield_receipt`, `remove_promise_yield_status`, or `remove_yield_id_mappings` (all defined in the same file at lines 214-334) for the deleted `account_id`. These four TrieKey variants (`PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`) are all keyed with `receiver_id`/`account_id` equal to the account being deleted, exactly as the setters (`set_promise_yield_receipt`, `set_promise_yield_status`, `set_yield_id_mapping`) at lines 200-297 write them.

Meanwhile, the promise-yield timeout queue entry (`TrieKey::PromiseYieldTimeout`) created by `enqueue_promise_yield_timeout` (lines 182-198) is stored in a *global*, index-based queue unrelated to any per-account trie subtree, and simply records `account_id`, `data_id`, and `expires_at`. This queue entry is not cleaned up when the account is deleted (deletion logic in `remove_account` has no knowledge of, or interaction with, this queue).

Exploit flow:
1. Attacker deploys a contract on account `B` that calls `promise_yield_create` (populating `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` all keyed to `B`, and enqueuing a `PromiseYieldTimeout` entry referencing `B`).
2. Attacker submits `DeleteAccount(B)` before the yield resumes or times out. `remove_account` runs, but leaves the four Promise-Yield trie entries in place because they're absent from its clear set.
3. Attacker submits `CreateAccount(B)` and deploys a new/different contract to the same `account_id`.
4. When the block height reaches `expires_at`, the global timeout-processing path (in `runtime/runtime/src/lib.rs`) looks up the surviving `PromiseYieldReceipt`/`PromiseYieldStatus` still present under `B` and resumes/executes it with `predecessor_id == receiver_id == B`, even though this state has no relationship to the currently deployed contract.

None of the standard checks (signature/nonce validation, access-key permission, gas metering, storage staking) intervene here because `DeleteAccount` and `CreateAccount` are both individually valid, ordinary actions; the bug is a missing state-cleanup step, not an authorization bypass at the action level.

I was unable to fully trace, within available tool budget, (a) whether `DeleteAccount` validation in `runtime/runtime/src/actions.rs` blocks deletion when pending yield state exists for that account, and (b) the exact resume-timeout code path in `runtime/runtime/src/lib.rs` (predecessor/receiver assignment during timeout resolution) to confirm the precise semantics of what code runs against the new account. These would need direct confirmation via a runtime/test-loop integration test before treating downstream impact (e.g., "double resolution" or arbitrary code execution attribution) as fully proven; the storage-hygiene defect in `remove_account` itself, however, is directly confirmed in the source.

### Impact Explanation
Confirmed: stale Promise-Yield-prefixed trie rows survive `remove_account`, meaning promise-yield callback/resolution data for a deleted account is never cleaned up and can persist to be acted upon by the global timeout queue after account recreation. This is a state-integrity/determinism defect (authorization exactness violation: callback execution attributed to unrelated re-created account) rather than a directly demonstrated fund-theft primitive in this trace. Given NEAR bounty categories, this most closely maps to "authorization escalation across accounts or promises" if the downstream resume path indeed executes against the new account's context — but I could not fully confirm the resume-execution semantics to elevate this to a certain fund-loss/consensus-divergence claim within this investigation.

### Likelihood Explanation
Preconditions are fully attacker-controlled and cheap: deploying a contract, calling `promise_yield_create`, then `DeleteAccount`/`CreateAccount` on the same account are all ordinary, unprivileged transactions available to any funded account. The only unverified precondition is whether `DeleteAccount` is permitted at all while an account has outstanding yield state — this must be checked before treating the attack as fully repeatable.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also purge all Promise-Yield-prefixed rows for the deleted account: iterate/remove `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, and `TrieKey::DataIdToYieldId` entries scoped to `account_id` (analogous to how `ContractData` and access keys are prefix-iterated and removed), and additionally ensure the global `PromiseYieldTimeout` queue processing path treats timeouts referencing a since-deleted-and-recreated account as no-ops (e.g., by validating the yield state actually exists before resuming, which it already implicitly must do via `get_promise_yield_receipt`/`get_promise_yield_status`, but this only helps if those getters correctly return `None` — which is prevented by the very fact these rows are never cleared).

### Proof of Concept
Unit test in `core/store/src/utils/mod.rs` (or a `test_utils.rs` companion):
1. Create a `TrieUpdate`.
2. Call `set_yield_id_mapping(&mut su, &account_b, yield_id, data_id)`, `set_promise_yield_receipt(&mut su, &receipt_for_b)`, `set_promise_yield_status(&mut su, &account_b, data_id, status)`.
3. Call `remove_account(&mut su, &account_b)`.
4. Assert:
   - `has_promise_yield_receipt(&su, account_b.clone(), data_id)? == true` (currently true, should be `false`)
   - `has_promise_yield_status(&su, &account_b, data_id)? == true` (currently true, should be `false`)
   - `has_yield_id_mapping(&su, &account_b, yield_id)? == true` (currently true, should be `false`)
   - `get_yield_id_for_data_id(&su, &account_b, data_id)?.is_some() == true` (currently true, should be `false`)

A follow-up runtime/test-loop integration test (extending `test-loop-tests/src/tests/yield_timeouts.rs` or `yield_resume.rs`) should additionally: create a yield on account `B`, delete `B`, recreate `B` with a different contract, advance blocks to `expires_at`, and observe whether the timeout-resolution path touches/executes against the recreated `B`'s state — this is required to fully confirm the downstream execution-attribution impact beyond the storage-hygiene defect already proven above.