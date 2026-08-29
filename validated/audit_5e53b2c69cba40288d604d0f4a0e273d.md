### Title
Stale self-callback `PromiseYieldReceipt`/`PromiseYieldStatus` rows survive `DeleteAccount` and execute against a recreated account - (File: `runtime/runtime/src/actions.rs`, `core/store/src/utils/mod.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`remove_account` (called from `action_delete_account`) removes `Account`, `ContractCode`, `AccessKey`/gas-key, and `ContractData` rows, but never removes `TrieKey::PromiseYieldReceipt`/`TrieKey::PromiseYieldStatus` rows keyed by the deleted account, nor does `action_delete_account` check for pending yields before allowing deletion. The globally-indexed `PromiseYieldTimeout` queue entry (keyed only by index, carrying `account_id`/`data_id`) is also untouched. When the account name is later recreated by anyone, the still-present timeout fires and executes the orphaned self-callback receipt against the new owner's contract/account state.

### Finding Description
`promise_yield_create` (`runtime/runtime/src/ext.rs:353-368`) always creates the yield receipt with `receiver_id == self.context.current_account_id` — PromiseYield receipts are self-callbacks, confined to the same account (documented in `protocol-model/spec/cross-shard-congestion.md:284-290`). Creation writes:
- `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` (`core/store/src/utils/mod.rs:200-211`)
- `TrieKey::PromiseYieldStatus { receiver_id, data_id }` (`core/store/src/utils/mod.rs:260-271`)
- a `TrieKey::PromiseYieldTimeout { index }` entry containing `{account_id, data_id, expires_at}` in a per-shard FIFO queue keyed purely by `index`, not by account (`core/primitives/src/trie_key.rs:239-247`, `core/store/src/utils/mod.rs:181-198`).

`action_delete_account` (`runtime/runtime/src/actions.rs:314-391`) only checks `MAX_ACCOUNT_DELETION_STORAGE_USAGE` and gas-key balance-to-burn before calling `remove_account` (`core/store/src/utils/mod.rs:504-575`). `remove_account` removes `Account`, `ContractCode`, access/gas keys, and `ContractData`, but never iterates or removes `PromiseYieldReceipt`/`PromiseYieldStatus` rows for the account. `check_actor_permissions`'s `DeleteAccountStaking` check only blocks deletion when `Account.locked != 0` — it has no knowledge of pending yields.

Consequently:
1. Attacker account `A` calls `promise_yield_create` with a chosen `method_name`/`args`/`gas` self-callback.
2. `A` immediately (or later) self-deletes via `DeleteAccount` (locked stake is 0, so `DeleteAccountStaking` never fires).
3. The `PromiseYieldReceipt{A, data_id}`, `PromiseYieldStatus{A, data_id}`, and the queued `PromiseYieldTimeout{index}` all survive in the trie, unreachable from `A`'s (now-removed) `Account` row.
4. Account name `A` is later recreated (implicit-account `Transfer` for 64-hex names, or `CreateAccount` by the parent for sub-accounts) by an unrelated party who deploys new contract code and/or keys.
5. When `apply_state.block_height` reaches `expires_at`, `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3104`) checks `state_update.contains_key(PromiseYieldReceipt{A, data_id})` — still `true` — and synthesizes a `PromiseResume{data: None}` receipt destined for account `A` (`lib.rs:3046-3097`).
6. Receipt processing (`lib.rs:1500-1562`) finds the stale `yield_receipt`, removes the leftover rows, and calls `apply_action_receipt` on the parked actions. Because `predecessor_id == receiver_id == account_id` (self-call semantics), any `assert_self()`-style privileged method gate on the *new* contract passes, and the attacker-chosen `method_name`/`args`/prepaid `gas` execute against the new owner's live account/contract state — none of which the attacker ever had authorization over.

No existing check (signature/nonce verification, `DeleteAccountStaking`, storage-usage cap, gas-key balance cap) inspects or blocks pending yield state at deletion time.

### Impact Explanation
This breaks authorization exactness: a receipt that is supposed to be a same-account, self-only callback ends up executing against a different, unrelated occupant of the account name with self-call privilege, after that occupant never created, authorized, or was even aware of the yield. Depending on the new contract's logic (e.g., admin/finalize methods gated by `predecessor_id == current_account_id`), this can trigger unauthorized state mutation or fund movement (e.g., attacker-chosen deposit/withdraw callback methods) — matching NEAR's "authorization escalation across accounts or promises" / "unexpected fund movement" bounty category.

### Likelihood Explanation
Fully reachable by an ordinary unprivileged account: it only requires calling `promise_yield_create` on one's own account, then a self-`DeleteAccount` with zero locked stake, and waiting for `yield_timeout_length_in_blocks` to elapse — all standard, permitted actions with no special access. The attacker fully controls the timing (create yield, delete before resume, then whoever reclaims the name later is affected). It is repeatable for any account name that can be vacated and reclaimed (implicit accounts are trivially reclaimable by anyone; sub-accounts require the same parent to recreate them, which narrows but does not eliminate the scenario since the parent itself, or a new key-holder it adds, becomes the "new owner").

### Recommendation
In `action_delete_account`/`remove_account`, before or during account removal, scan and remove any `PromiseYieldReceipt`/`PromiseYieldStatus` rows for the account (similar to how `AccessKey`/`ContractData` prefixes are iterated and removed in `core/store/src/utils/mod.rs:504-575`), and/or block `DeleteAccount` while any yield is pending for that account (analogous to the existing `DeleteAccountStaking` check). Additionally, `resolve_promise_yield_timeouts` should validate that the account still exists (or was not recreated after the yield was created) before dispatching the `PromiseResume`, to defend against any other path that might leave rows behind.

### Proof of Concept
Apply-path integration test (in `runtime/runtime/src/tests/apply.rs`, following the pattern of `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` at lines 4877-4954):
1. Deploy `rs_contract` to account `child.alice.near`, call `call_yield_create_return_promise` to create a pending yield (`PromiseYieldReceipt`/`PromiseYieldStatus`/`PromiseYieldTimeout` written).
2. Issue a self-`DeleteAccount` receipt for `child.alice.near` (locked stake 0) with a different beneficiary.
3. Recreate `child.alice.near` via `CreateAccount` from `alice.near`, deploy a *different* trivial contract, add a fresh access key.
4. Advance `apply_state.block_height` past `expires_at` and run `apply` so `resolve_promise_yield_timeouts` fires.
5. Assert: the synthesized `PromiseResume` executes the orphaned yield's `FunctionCall` against the recreated account, and that this mutates the new account's `Account`/state despite the new owner never having created or approved the yield — i.e., assert the timeout resolution *does* touch the new account's rows (proving the escalation), contrary to the expected invariant that it should not.