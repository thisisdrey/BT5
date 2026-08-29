### Title
`remove_account` clears none of the 4 account-scoped PromiseYield trie rows written by `promise_yield_create`/`promise_yield_create_with_id`, allowing a stale yield to execute self-privileged logic against an account recreated under the same id - (File: `core/store/src/utils/mod.rs`)

### Summary
`promise_yield_create`/`promise_yield_create_with_id` write four account-scoped `TrieKey`s under `receiver_id` — `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` — but `remove_account` (invoked by `DeleteAccountAction`) removes only `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData`, leaving all four promise-yield rows behind. Because NEAR allows an account id to be reused after deletion, the stale `PromiseYieldReceipt` can later be resumed by the non-account-scoped `PromiseYieldTimeout` queue and executed against whatever account now occupies that id, with the receipt's original self-call (`predecessor_id == receiver_id == account_id`) semantics intact.

### Finding Description
`core/store/src/utils/mod.rs:504-575` (`remove_account`) removes exactly these rows: [1](#0-0) 
It never calls `remove_promise_yield_receipt`, `remove_promise_yield_status`, or any yield-id-mapping removal helper, even though `TrieKey::get_account_id` classifies all four as account-scoped (`receiver_id`): [2](#0-1) 

`promise_yield_create` (host function) calls `create_promise_yield_receipt`, which writes `PromiseYieldStatus` immediately: [3](#0-2) 
and `create_promise_yield_receipt_with_id` additionally writes the `YieldIdToDataId`/`DataIdToYieldId` mappings: [4](#0-3) 
The full `PromiseYieldReceipt` (containing the actual `ActionReceipt`, including the `FunctionCall` action set up via `append_action_function_call_weight`) is persisted once the receipt is delivered to the receiver shard: [5](#0-4) 

The corresponding cleanup only happens on a normal resume path (`PromiseResume` with matching data, `lib.rs:1517-1521`) or timeout resolution — never in `remove_account`/`action_delete_account`: [6](#0-5) 

The timeout queue entry (`PromiseYieldTimeout`, correctly *not* account-scoped) is processed independently of account existence: [7](#0-6) 
It checks only `state_update.contains_key(&promise_yield_key, ...)` — the stale `PromiseYieldReceipt` left by the deleted account — and if present, forwards a `PromiseResume` receipt for `queue_entry.account_id`. Because `remove_account` never deleted this row, the check succeeds, and `apply_action_receipt` executes the stale receipt's `FunctionCall` (attacker-chosen `method_name`/`arguments`, self-targeted: `predecessor_id == receiver_id == account_id`) against whichever account currently exists under that id.

**Attacker flow:**
1. Attacker's account `A.near` calls `promise_yield_create` targeting itself with `method_name` set to a privileged, self-only method (e.g. one gated by `predecessor_account_id() == current_account_id()` in the deployed contract) and a large prepaid `gas`.
2. Before the yield resumes or times out, attacker submits `DeleteAccountAction` for `A.near`. `action_delete_account` → `remove_account` runs, but the `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id-mapping rows for `A.near` survive.
3. `A.near` becomes available; a third party (or the attacker again) creates a fresh account `A.near` and deploys a contract with self-privileged logic.
4. After `yield_timeout_length_in_blocks`, `resolve_promise_yield_timeouts` finds the still-present stale `PromiseYieldReceipt` and forwards a resume, causing the stale `FunctionCall` to execute as a self-call against the new occupant's contract — bypassing any `assert_self()`-style authorization since `predecessor_id` recorded on the stale receipt is literally the account id itself.

No existing check (`DeleteAccountWithLargeState`, `GasKeyBalanceTooHigh`, access-key/nonce checks) inspects promise-yield state before allowing deletion, and no size/staking limit blocks this.

### Impact Explanation
This is an authorization-escalation bug across the account-id boundary: a self-privileged action, planted by a previous occupant of an account id, executes with self-call trust against a later, unrelated occupant of the same id. This matches the "authorization escalation across accounts or promises" bounty category. It is a state-cleanliness/consensus-safe bug (it does not corrupt the trie root computation itself, since the rows simply persist), but its logical consequence is arbitrary code execution with elevated (self) trust on the victim's later-deployed contract.

### Likelihood Explanation
Preconditions are attacker-controlled up to account deletion (cheap: one function call + one `DeleteAccount` action), but exploitation to reach a *specific* victim requires that account id be re-claimed and have privileged self-only logic deployed within the yield timeout window (bounded, on the order of the configured `yield_timeout_length_in_blocks`). This narrows real-world exploitation mostly to griefing/targeted scenarios (e.g., squatting a soon-to-be-claimed short/premium account name), rather than a broadly repeatable theft primitive against arbitrary victims. The underlying code defect (asymmetric write/clear set) is fully deterministic and trivially reproducible.

### Recommendation
Extend `remove_account` (`core/store/src/utils/mod.rs`) to also iterate and remove `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` rows scoped to the deleted `account_id` (mirroring the existing access-key/contract-data iteration pattern), and update `RemoveAccountResult`/compute accounting accordingly. Alternatively/additionally, reject `DeleteAccountAction` when pending yields exist for the account.

### Proof of Concept
Minimal unit test in `core/store/src/utils/mod.rs` (or a sibling test module) demonstrating the write/clear mismatch:
```rust
#[test]
fn remove_account_leaves_promise_yield_rows() {
    let tries = TestTriesBuilder::new().build();
    let mut state_update = tries.new_trie_update(ShardUId::single_shard(), CryptoHash::default());
    let account_id: AccountId = "a.near".parse().unwrap();
    let data_id = CryptoHash::hash_bytes(b"data");
    let yield_id = YieldId::from_bytes([7u8; 32]);

    // Simulate the 4 account-scoped writes made by promise_yield_create[_with_id].
    set_promise_yield_status(&mut state_update, &account_id, data_id, PromiseYieldStatus::Yielded);
    set_promise_yield_receipt(&mut state_update, /* dummy Receipt with receiver_id = account_id, data_id */);
    set_yield_id_mapping(&mut state_update, &account_id, yield_id, data_id); // sets both YieldIdToDataId & DataIdToYieldId
    state_update.commit(StateChangeCause::InitialState);

    let written = [
        has_promise_yield_status(&state_update, &account_id, data_id).unwrap(),
        has_promise_yield_receipt(&state_update, account_id.clone(), data_id).unwrap(),
        has_yield_id_mapping(&state_update, &account_id, yield_id).unwrap(),
        get_yield_id_for_data_id(&state_update, &account_id, data_id).unwrap().is_some(),
    ];
    assert_eq!(written.iter().filter(|b| **b).count(), 4, "expected all 4 rows written");

    remove_account(&mut state_update, &account_id).unwrap();
    state_update.commit(StateChangeCause::InitialState);

    let cleared = [
        !has_promise_yield_status(&state_update, &account_id, data_id).unwrap(),
        !has_promise_yield_receipt(&state_update, account_id.clone(), data_id).unwrap(),
        !has_yield_id_mapping(&state_update, &account_id, yield_id).unwrap(),
        get_yield_id_for_data_id(&state_update, &account_id, data_id).unwrap().is_none(),
    ];
    // Demonstrates the asymmetry: 4 written, 0 cleared.
    assert_eq!(cleared.iter().filter(|b| **b).count(), 0, "remove_account cleared none of the 4 rows");
}
```
For end-to-end confirmation of exploitability, an integration/test-loop test can: (1) create a self-targeted yield on `a.near` with a privileged `FunctionCall` method_name, (2) delete `a.near`, (3) recreate `a.near` and deploy a contract exposing a method gated on `predecessor_account_id() == current_account_id()`, (4) advance blocks past `yield_timeout_length_in_blocks`, and (5) assert the privileged method executed on the new deployment without the new owner ever calling it directly.

### Citations

**File:** core/store/src/utils/mod.rs (L504-510)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/primitives/src/trie_key.rs (L605-617)
```rust
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::BufferedReceiptIndices => None,
            TrieKey::BufferedReceipt { .. } => None,
            TrieKey::BandwidthSchedulerState => None,
            TrieKey::BufferedReceiptGroupsQueueData { .. } => None,
            TrieKey::BufferedReceiptGroupsQueueItem { .. } => None,
            // Even though global contract code might be deployed under account id, it doesn't
            // correspond to the data stored for that account id, so always returning None here.
            TrieKey::GlobalContractCode { .. } => None,
            TrieKey::GlobalContractNonce { .. } => None,
            TrieKey::PromiseYieldStatus { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::YieldIdToDataId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DataIdToYieldId { receiver_id, .. } => Some(receiver_id.clone()),
```

**File:** runtime/runtime/src/ext.rs (L353-369)
```rust
    fn create_promise_yield_receipt(
        &mut self,
        receiver_id: AccountId,
    ) -> Result<(ReceiptIndex, CryptoHash), VMLogicError> {
        let input_data_id = self.generate_data_id();
        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok((receipt_index, input_data_id))
    }
```

**File:** runtime/runtime/src/ext.rs (L371-400)
```rust
    fn create_promise_yield_receipt_with_id(
        &mut self,
        receiver_id: AccountId,
        user_yield_id: YieldId,
    ) -> Result<Option<(ReceiptIndex, CryptoHash)>, VMLogicError> {
        // Check for duplicate yield_id in trie. TrieUpdate also reflects writes from earlier
        // calls within the same function call, so this also catches in-transaction duplicates.
        if has_yield_id_mapping(self.trie_update, &receiver_id, user_yield_id)
            .map_err(wrap_storage_error)?
        {
            return Ok(None);
        }

        let input_data_id = self.generate_data_id();

        // Store bidirectional yield_id <-> data_id mappings
        set_yield_id_mapping(&mut self.trie_update, &receiver_id, user_yield_id, input_data_id);

        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok(Some((receipt_index, input_data_id)))
    }
```

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```

**File:** runtime/runtime/src/lib.rs (L3009-3068)
```rust
fn resolve_promise_yield_timeouts(
    processing_state: &mut ApplyProcessingReceiptState,
    receipt_sink: &mut ReceiptSink,
    compute_limit: u64,
) -> Result<ResolvePromiseYieldTimeoutsResult, RuntimeError> {
    let mut state_update = &mut processing_state.state_update;
    let total = &mut processing_state.total;
    let apply_state = &processing_state.apply_state;

    let mut promise_yield_indices: PromiseYieldIndices =
        get(state_update, &TrieKey::PromiseYieldIndices)?.unwrap_or_default();
    let initial_promise_yield_indices = promise_yield_indices.clone();
    let mut new_receipt_index: usize = 0;

    let mut processed_yield_timeouts = vec![];
    let yield_processing_start = std::time::Instant::now();
    while promise_yield_indices.first_index < promise_yield_indices.next_available_index {
        if total.compute >= compute_limit || state_update.trie.check_proof_size_limit_exceed() {
            break;
        }

        let queue_entry_key =
            TrieKey::PromiseYieldTimeout { index: promise_yield_indices.first_index };

        let queue_entry =
            get::<PromiseYieldTimeout>(state_update, &queue_entry_key)?.ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "PromiseYield timeout queue entry #{} should be in the state",
                    promise_yield_indices.first_index
                ))
            })?;

        // Queue entries are ordered by expires_at
        if queue_entry.expires_at > apply_state.block_height {
            break;
        }

        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
            let new_receipt_id = create_receipt_id_from_receipt_id(
                &queue_entry.data_id,
                apply_state.block_height,
                new_receipt_index,
            );
            new_receipt_index += 1;

            // Create a PromiseResume receipt to resolve the timed-out yield.
            let resume_receipt = Receipt::V0(ReceiptV0 {
                predecessor_id: queue_entry.account_id.clone(),
                receiver_id: queue_entry.account_id.clone(),
                receipt_id: new_receipt_id,
                receipt: ReceiptEnum::PromiseResume(DataReceipt {
                    data_id: queue_entry.data_id,
                    data: None,
                }),
            });
```

**File:** runtime/runtime/src/actions.rs (L364-371)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
```
