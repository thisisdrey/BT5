### Title
Postponed-receipt state (`ReceivedData`/`PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt`) survives `DeleteAccount` and executes against a recreated account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` only deletes `Account`, `ContractCode`, `AccessKey`/gas-key, and `ContractData` trie entries for a deleted account; it never touches the `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt` entries that are keyed by the same `receiver_id`. An attacker who owns an account, arranges a self-targeted `ActionReceipt` with two `input_data_ids` (delivering only one before deleting the account), can leave a "time-bomb" postponed receipt in state that survives the deletion. When the account name is later reused/recreated (e.g. by a registrar/dApp that recycles subaccount names) and the withheld second `DataReceipt` finally arrives, the runtime resolves the stale bookkeeping and executes the old, pre-authorized receipt's actions (e.g. `AddKey`, `DeployContract`) against the new account, since these actions only require `predecessor_id == receiver_id`, which trivially still holds for the recycled account id.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) is called by `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) on `DeleteAccountAction`. It removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- `TrieKey::AccessKey` / gas-key nonces
- `TrieKey::ContractData` [1](#0-0) 

It never enumerates or removes `TrieKey::ReceivedData`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` for the account, even though all four are keyed by `receiver_id` just like the columns that are cleaned up. [2](#0-1) 

These entries are created during normal receipt-matching processing: `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) writes `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` when an `ActionReceipt`'s `input_data_ids` are not all satisfied yet. [3](#0-2) 

`process_receipt`'s `Data` branch (`runtime/runtime/src/lib.rs:1367-1444`) writes `ReceivedData`, decrements `PendingDataCount`, and — once it reaches 0 — fetches and executes the stored `PostponedReceipt` unconditionally, with no check on whether the receiving account was deleted and recreated in the interim. [4](#0-3) 

`check_account_existence` (`runtime/runtime/src/actions.rs:787-855`) only rejects `CreateAccount` if `account.is_some()`; a deleted account (`account == None`) can be freely recreated without any check for leftover `ReceivedData`/`PostponedReceipt` records tied to the same account id. [5](#0-4) 

Exploit flow:
1. Attacker (owning account `x.near`, e.g. as a subaccount they were granted, or as their own account) issues a self-targeted `ActionReceipt` (predecessor_id == receiver_id == `x.near`) with two `input_data_ids` and an action list that requires `predecessor_id == receiver_id` (e.g. `AddKeyAction` or `DeployContractAction`) — buildable via the standard promise batch API (`promise_and`/batch action creation), the same mechanism exercised by `call_promise` in the test contract.
2. Attacker resolves only one of the two data dependencies. `PostponedReceiptId{x.near, id2}`, `PendingDataCount{x.near, receipt_id}=1`, and `PostponedReceipt{x.near, receipt_id}` remain in state; `ReceivedData{x.near, id1}` also remains.
3. Attacker submits `DeleteAccountAction` on `x.near`. `action_delete_account`/`remove_account` clears the `Account`/keys/code/data but leaves the four receipt-matching entries untouched.
4. `x.near` is recreated (e.g. by a registrar contract issuing subaccount names to a new, unrelated user) via `CreateAccountAction`, which succeeds because `account_id` is currently absent — the check has no knowledge of the dangling postponed-receipt bookkeeping.
5. Attacker (or anyone) delivers the second, previously withheld `DataReceipt` with `data_id = id2`. `process_receipt` finds the still-present `PostponedReceiptId`, decrements `PendingDataCount` to 0, loads the still-present `PostponedReceipt`, and executes it via `apply_action_receipt` against the *new* account. Because the receipt's embedded `predecessor_id`/`receiver_id` are both `x.near`, the self-authorization check for `AddKey`/`DeployContract`/etc. passes trivially, executing the attacker's pre-baked action against the new owner's account without any signature or key check from the new owner.

No existing check (signature/nonce/access-key/action validation/storage staking) intervenes at step 5, because receipts are pre-authorized at creation time and never re-verified against the current account owner at delivery time; this design assumption breaks once the account identity has changed underneath the receipt due to a delete+recreate cycle.

### Impact Explanation
This is an authorization-escalation / determinism bug: `DeleteAccount` is documented and expected to purge *all* state associated with an account, but it leaves cross-shard/data-dependent receipt state behind. This allows a formerly-authorized (self) action — chosen entirely by the previous account owner — to execute against a subsequently and independently created/owned account, enabling arbitrary privileged actions (`AddKey`, `DeployContract`, `Stake`, `DeleteAccount`, etc., all of which only require `predecessor_id == receiver_id`) against the new owner without their consent. This maps to NEAR's "authorization escalation across accounts or promises" bounty category and also violates the state-determinism/exactness invariant that account deletion removes all account-scoped state.

### Likelihood Explanation
The attacker needs no privileged access — only the ability to sign transactions, deploy/call their own contract, and control an account whose name is later recycled (e.g. through a registrar/dApp pattern that deletes and reissues named subaccounts, or any workflow where an account id can be recreated after deletion). Constructing the postponed-receipt/data-receipt split is fully doable with standard promise batching primitives (as exercised by existing test-loop-tests such as `test_instant_delete_account`), and the attacker fully controls the timing of the second `DataReceipt`'s delivery, so the exploit is deterministic and repeatable. The main precondition — a real party recreating the exact same account id after the attacker deletes it — depends on an account-recycling usage pattern, but the underlying protocol bug (leftover `ReceivedData`/`PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` after `DeleteAccount`) is unconditionally present and reachable by any unprivileged user.

### Recommendation
Extend `remove_account` (`core/store/src/utils/mod.rs`) to also enumerate and remove `TrieKey::ReceivedData`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` entries for the deleted `account_id` (mirroring the existing access-key/contract-data prefix iteration), or alternatively make `process_receipt`'s postponed-receipt resolution path validate that the account referenced by a resolved `PostponedReceipt` has not been deleted/recreated since the receipt was postponed (e.g. by tagging postponed receipts with the account's creation nonce/generation and rejecting execution on mismatch).

### Proof of Concept
Runtime/test-loop integration test:
1. Create account `x.near` (or a subaccount under an attacker-controlled parent) with a deployed test contract supporting batched promise actions (e.g. the existing `rs_contract`'s `call_promise`).
2. From `x.near`, call a method that creates: (a) promise `p1` calling itself (or another contract) that will return quickly, (b) promise `p2` calling another contract that the attacker controls and can trigger at will, (c) `promise_and([p1, p2])` followed by a batched receipt on `x.near` containing `AddKeyAction` (full access key controlled by attacker) or `DeployContractAction`.
3. Let `p1`'s data receipt resolve (deliver naturally), leaving the joint receipt postponed with `PendingDataCount == 1` for `p2`. Do **not** trigger `p2` yet.
4. Submit `DeleteAccountAction` for `x.near`; assert the account is deleted, but verify the trie still contains `PostponedReceipt{x.near, receipt_id}`, `PendingDataCount{x.near, receipt_id}`, and `PostponedReceiptId{x.near, id2}` (via low-level trie inspection or `TrieUpdate` iteration over those columns).
5. Recreate `x.near` as a fresh account (e.g. via a different signer acting as the sub-account's parent), with a fresh contract/keys — assert the new account has no `AddKeyAction` key / different code hash than what was queued in step 2.
6. Trigger `p2`'s callback so its `DataReceipt` is delivered to `x.near`.
7. Assert that the queued action from step 2 (new full-access key present, or contract code hash changed) now applies to the *new* `x.near` account, despite no `AddKey`/`DeployContract` transaction being signed by the new account owner — confirming unauthorized state mutation via the stale postponed receipt.

### Citations

**File:** core/store/src/utils/mod.rs (L505-513)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;
```

**File:** core/primitives/src/trie_key.rs (L192-219)
```rust
    /// Used to store `primitives::receipt::ReceivedData` struct for a given receiver's `AccountId`
    /// of `DataReceipt` and a given `data_id` (the unique identifier for the data).
    /// NOTE: This is one of the input data for some action receipt.
    /// The action receipt might be still not be received or requires more pending input data.
    ReceivedData {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::RECEIVED_DATA,
    /// Used to store receipt ID `primitives::hash::CryptoHash` for a given receiver's `AccountId`
    /// of the receipt and a given `data_id` (the unique identifier for the required input data).
    /// NOTE: This receipt ID indicates the postponed receipt. We store `receipt_id` for performance
    /// purposes to avoid deserializing the entire receipt.
    PostponedReceiptId {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::POSTPONED_RECEIPT_ID,
    /// Used to store the number of still missing input data `u32` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PendingDataCount {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::PENDING_DATA_COUNT,
    /// Used to store the postponed receipt `primitives::receipt::Receipt` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PostponedReceipt {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::POSTPONED_RECEIPT,
```

**File:** runtime/runtime/src/lib.rs (L1396-1444)
```rust
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
```

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
        }
```

**File:** runtime/runtime/src/actions.rs (L794-801)
```rust
    match action {
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
```
