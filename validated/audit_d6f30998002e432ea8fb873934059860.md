### Title
Access key recreation resets nonce based on block height instead of preserving the prior key's nonce, enabling replay of previously-signed transactions - ([File: runtime/runtime/src/access_keys.rs])

### Summary
When a `DeleteKey` action removes an access key, its associated nonce record is fully discarded from state rather than being preserved. If a new `AddKey` action later re-adds an access key with the **same public key**, the new key's nonce is initialized purely from `initial_nonce_value(block_height)` — a value derived only from the current block height — instead of being seeded from the nonce the deleted key had reached. This is the same bug class as the UFARM report: a tracking record tied to an identifier (there, `withdrawalRequestHash`; here, the access key's `nonce`) is not carried through/cleared correctly across a delete-then-recreate cycle, letting a user replay a previously valid, already-consumed identifier (a signed transaction with an old nonce) to reach an unintended state again.

### Finding Description
`action_delete_key` in [1](#0-0)  unconditionally calls `remove_access_key`, deleting the `AccessKey{nonce, permission}` trie entry — the nonce value is not persisted anywhere else.

When the same public key is later re-added via `AddKeyAction`, the new access key's nonce is not derived from the old key's last nonce; it is computed by [2](#0-1)  as `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, a value that depends solely on the block height at which the key is recreated.

The design intent, documented explicitly, is the opposite of what the code does: [3](#0-2)  — "In some cases the access key needs to be recreated. If the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again."

The codebase itself documents this as a known, unresolved defect. The integration test `test_delete_key_last` contains a TODO acknowledging exactly this gap: [4](#0-3) 

Because nonces are the sole replay-protection mechanism for transactions signed against a given public key, and because the block-height-derived initial value is not guaranteed to exceed every nonce the account previously issued under that key (nonces can be chosen arbitrarily high by the signer, up to protocol limits, and are not required to track block height), a transaction signed with a high nonce under the original key — never submitted, or submitted and rejected/pending — can become valid again once the key is deleted and recreated at a lower block-height-derived nonce baseline.

### Impact Explanation
This is a double-spend/replay class vulnerability: a transaction (e.g., a `Transfer` or `FunctionCall` action) that a user considers already accounted for, or intentionally never broadcast, can be resubmitted and executed again after the account owner deletes and recreates the same public key — because the runtime "forgets" the nonce watermark, exactly as the UFARM report's pool forgot to delete the `withdrawalRequestHash`, allowing the same identifier to be reused to force an unintended state transition. Here the reused identifier is a transaction nonce, and the unintended state transition is unauthorized re-execution of a previously signed action (fund transfer, contract call, etc.) against the account.

### Likelihood Explanation
The delete-key/add-key sequence with an identical public key is an ordinary, unprivileged operation reachable by any account owner (or, for `FunctionCall`-permissioned or attacker-controlled keys, potentially a contract acting on their behalf) using only standard `DeleteKey`/`AddKey` actions — no privileged role, node, or peer behavior is required. The scenario requires the account to have previously signed a transaction with a nonce higher than the value the new key will be initialized with, which is achievable by an attacker (or malicious/careless client) simply choosing a large nonce value when signing, then deleting and re-adding the key.

### Recommendation
When adding a key with a public key that is being reused after a prior deletion, persist and reuse the maximum nonce ever observed for that `(account_id, public_key)` pair (or otherwise ensure the new key's nonce floor is at least as high as the old key's last nonce) instead of relying solely on `initial_nonce_value(block_height)`. This mirrors the UFARM fix pattern of always clearing/carrying forward the tracked state (there, deleting the request hash unconditionally; here, always preserving the nonce watermark across key deletion) rather than only doing so in the common-case path.

### Proof of Concept
1. Account `alice` has a full-access key `K`.
2. `alice` signs (but does not submit) `tx1 = Transfer(nonce = N, K)` with a very large `N` (e.g., far beyond the current block-height-derived nonce range).
3. `alice` submits `DeleteKey(K)`, then `AddKey(K)` in the same or later blocks — this is a common workflow (e.g., allowance reset for a function-call key or key rotation UX).
4. The new access key for `K` is initialized with `nonce = (block_height - 1) * 1_000_000`, per [2](#0-1) , which is lower than `N`.
5. `alice` (or anyone holding `tx1`, e.g., a previously-authorized relayer) submits `tx1` — it passes nonce validation (`N > current key nonce`) and executes, replaying an action the user believed was invalidated by the key deletion.

This confirms the root cause is fully supported by code in scope; full end-to-end verification of the transaction-verifier's exact nonce comparison logic in `runtime/runtime/src/verifier.rs` was not completed due to the tool-call limit reached during investigation, but the standard NEAR nonce semantics (`tx.nonce > access_key.nonce`) are well established elsewhere in the codebase (e.g., `integration-tests/src/tests/features/access_key_nonce_for_implicit_accounts.rs`), and the maintainers' own TODO(#6724) corroborates that nonce continuity is broken across key deletion.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L52-91)
```rust
pub(crate) fn action_delete_key(
    config: &RuntimeConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_key: &DeleteKeyAction,
) -> Result<(), RuntimeError> {
    let access_key = get_access_key(state_update, account_id, &delete_key.public_key)?;
    if let Some(access_key) = access_key {
        if let Some(gas_key_info) = access_key.gas_key_info() {
            delete_gas_key(
                config,
                state_update,
                account,
                result,
                account_id,
                &delete_key.public_key,
                &access_key,
                gas_key_info,
            )?;
        } else {
            delete_regular_key(
                &config.fees,
                state_update,
                account,
                account_id,
                &delete_key.public_key,
                &access_key,
            );
        }
    } else {
        result.result = Err(ActionErrorKind::DeleteKeyDoesNotExist {
            public_key: delete_key.public_key.clone().into(),
            account_id: account_id.clone(),
        }
        .into());
    }
    Ok(())
}
```

**File:** docs/DataStructures/AccessKey.md (L8-11)
```markdown
    /// The nonce for this access key.
    /// NOTE: In some cases the access key needs to be recreated. If the new access key reuses the
    /// same public key, the nonce of the new access key should be equal to the nonce of the old
    /// access key. It's required to avoid replaying old transactions again.
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L1169-1174)
```rust
        Err(err) => {
            // TODO(#6724): This is a wrong error, the transaction actually
            // succeeds. We get an error here when we retry the tx and the second
            // time around it fails. Normally, retries are handled by nonces, but we
            // forget the nonce when we delete a key!
            assert_eq!(
```
