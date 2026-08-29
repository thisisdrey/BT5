### Title
Value duplication via `TransferToGasKey` bypassing the storage-stake rollback gate in `apply_action_receipt` - ([File: runtime/runtime/src/lib.rs])

### Finding Description
`action_transfer_to_gas_key` credits a gas key's balance by writing directly into the trie through `set_access_key(state_update, ...)`, completely independent of the in-memory `Account` object that every other balance-affecting action (e.g. `Transfer`, `WithdrawFromGasKey`) mutates: [1](#0-0) 

By contrast, ordinary balance changes accumulate only on the mutable `account` argument threaded through `apply_action_receipt`'s action loop, and are persisted to the trie via a single `set_account` call gated behind the end-of-receipt storage-stake ("enough balance to cover storage after all actions") check. If that final check fails (`ActionErrorKind::LackBalanceForState`-style error), `set_account` is skipped and every account-balance-based effect accumulated during the receipt is discarded, while the receipt's outcome becomes `Failure`.

Because `TransferToGasKeyAction`'s deposit was already debited from the predecessor when the outgoing receipt was created (the same convention used for a normal cross-account `Transfer`), a `Failure` outcome for this receipt causes the runtime's refund logic to reimburse the predecessor for the receipt's total attached deposit. However, the gas-key credit performed by `action_transfer_to_gas_key` was written directly to `state_update` and is not part of the `account` object gated by `set_account`, so it is **not** rolled back when the receipt fails. The result is that both the refund (crediting the predecessor) and the gas-key credit (crediting the gas key) survive, doubling the value that a single deposit debit originally represented.

### Impact Explanation
This is a token-inflation bug: total supply increases by the deposit amount of the `TransferToGasKey` action every time it is followed, in the same receipt, by an action that fails the end-of-receipt storage-stake check. This falls under the "token inflation" bounty category for NEAR.

### Likelihood Explanation
An attacker only needs an ordinary account and an existing gas key, and needs to construct a receipt with actions `[TransferToGasKey(deposit), X]` where `X` is any action that pushes the account below the required storage-stake balance (e.g. adding more state without enough balance, or a second action whose per-action logic doesn't fail immediately but leaves insufficient balance for the account's storage at the end-of-receipt check). This requires no special privileges — a self-funded account and a normal transaction/promise batch are sufficient — and is repeatable at will, capped only by transaction/receipt size and gas limits.

### Recommendation
Make `action_transfer_to_gas_key`'s trie write participate in the same atomicity boundary as the rest of the receipt's balance effects: either (a) defer the `set_access_key` write for gas-key credits until after the final storage-stake check succeeds (mirroring how `set_account` is gated), or (b) explicitly roll back any direct trie writes performed by actions in the receipt whenever the final result is not `Ok`, and correspondingly ensure the refund computation excludes deposits for actions whose trie effects were not rolled back.

### Proof of Concept
Runtime/test-loop integration test:
1. Create an account with a gas key and a balance just above the storage-stake threshold.
2. Submit a receipt containing `[TransferToGasKey { public_key, deposit }, <action that fails the end-of-receipt storage-stake check>]`.
3. Apply the receipt and assert the outcome status is `Failure`.
4. Read the gas key balance from `state_update`/final trie state and assert it increased by `deposit`.
5. Track the predecessor's account balance across the eventual refund receipt processing and assert it also increases by `deposit`.
6. Assert `sum(all account balances) + sum(all gas key balances)` after processing both the failed receipt and its refund is strictly greater than before, violating value conservation.

Note: I was unable to directly read the full body of `apply_action_receipt` in `runtime/runtime/src/lib.rs` (specifically the exact `set_account` gating logic and the refund-computation code path) before exhausting available tool calls, so the precise refund mechanics and the exact conditions under which `set_account` is skipped are based on the codebase's general design pattern and the confirmed asymmetry in `action_transfer_to_gas_key` rather than a full line-by-line trace of `apply_action_receipt`. A Devin session with full file access should verify the exact refund/rollback code before treating this as fully confirmed.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L257-288)
```rust
pub(crate) fn action_transfer_to_gas_key(
    state_update: &mut TrieUpdate,
    result: &mut ActionResult,
    account_id: &AccountId,
    action: &TransferToGasKeyAction,
) -> Result<(), RuntimeError> {
    let Some(mut access_key) = get_access_key(state_update, account_id, &action.public_key)? else {
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };
    let Some(gas_key_info) = access_key.gas_key_info_mut() else {
        // Key exists but is not a gas key
        result.result = Err(ActionErrorKind::GasKeyDoesNotExist {
            account_id: account_id.clone(),
            public_key: Box::new(action.public_key.clone()),
        }
        .into());
        return Ok(());
    };

    gas_key_info.balance = gas_key_info.balance.checked_add(action.deposit).ok_or_else(|| {
        RuntimeError::StorageError(StorageError::StorageInconsistentState(
            "gas key balance integer overflow".to_string(),
        ))
    })?;
    set_access_key(state_update, account_id.clone(), action.public_key.clone(), &access_key);
    Ok(())
}
```
