### Title
Access-key nonce rollback via same-block DeleteKey+AddKey enables nonce/transaction replay - ([File: runtime/runtime/src/access_keys.rs])

### Summary
`initial_nonce_value(block_height)` returns `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, a value that depends only on the current block height, not on any prior nonce state of the key. `add_regular_key` unconditionally resets `access_key.nonce` to this value whenever an `AddKey` action is applied. Because block height is constant for every transaction executed within the same block, an attacker can exhaust a key's nonce toward a high value and then, in the same block, delete and re-add the same key to reset its stored nonce back down to the block's baseline, reopening the nonce window that was already consumed.

### Finding Description
`initial_nonce_value` is defined as: [1](#0-0) 

`add_regular_key` uses it to overwrite the access key's stored nonce on every `AddKeyAction`, with no consideration of any nonce previously consumed by the same public key at the same account: [2](#0-1) 

`action_add_key`, invoked from the runtime's main action dispatcher, rejects the `AddKey` only if a key with that public key currently exists — it does not check whether that public key existed and was recently deleted with a higher nonce at the same block height: [3](#0-2) [4](#0-3) 

`action_delete_key`/`delete_regular_key` unconditionally removes the access key record (including its nonce) from the trie: [5](#0-4) [6](#0-5) 

`check_actor_permissions` only requires that the actor be the account itself for `DeleteKey`/`AddKey`, so a single transaction signed with key `K` can contain `[Action::DeleteKey(K), Action::AddKey(K, same_pubkey)]`, sequentially deleting then re-creating the same key within its own execution: [7](#0-6) 

The documented purpose of the `(block_height - 1) * 1e6` reseed is exactly to prevent nonce/hash collision when a key is deleted and recreated (near/nearcore#3779), and this is validated by `test_transaction_hash_collision_for_near_implicit_account_fail`. However, in that regression test the delete and the recreate happen several blocks apart (`blocks_number = 5` between steps), so `initial_nonce_value` at recreation time is strictly larger than the value at first creation: [8](#0-7) 

The mitigation's soundness implicitly relies on block height increasing between deletion and re-creation. If deletion and re-creation of the same key happen at the **same** block height `h` (fully achievable in a single transaction or within the same chunk), `initial_nonce_value(h)` computes to the identical baseline `(h-1)*1e6` both times — it does not account for nonces already consumed by transactions processed earlier in that same block. This rolls the stored access-key nonce backward from `X` (the highest nonce consumed so far in block `h`) down to `(h-1)*1e6`, re-opening the entire nonce interval `((h-1)*1e6, X]` for reuse. Any previously-signed (but not yet chain-recorded, e.g. withheld by the attacker or dropped from another node's mempool) transaction using nonce in that interval becomes acceptable again by `get_signer_and_access_key`/nonce verification, since the check compares only against the currently stored `access_key.nonce`, which the attacker just rolled back.

### Impact Explanation
This breaks the "no transaction executes twice" nonce-monotonicity invariant for the attacker's own access key within a single block, enabling double execution of an old, previously signed transaction (double-spend/replay category). While the direct funds movement is between accounts the attacker controls or previously authorized, this primitive can be leveraged to replay any previously-signed transaction (e.g., a signed payment to a merchant/counterparty who accepted the first execution as final) a second time using the same access key and nonce, causing double execution of a transaction the counterparty believed was singular and final.

### Likelihood Explanation
Preconditions are attacker-controlled and cheap: one funded account with a full-access key, enough gas/balance to include `N+1` transactions/actions in a single block (epoch length only needs to keep block height constant across the sequence, which is naturally true for consecutive transactions accepted into the same chunk). No validator, node-operator, or privileged access is required — this is achievable purely as an ordinary RPC client submitting normal signed transactions and a `DeleteKey`+`AddKey` action pair.

### Recommendation
`initial_nonce_value`/`add_regular_key` should not blindly reset the nonce to `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` regardless of prior state. When re-adding a key with a public key that was deleted earlier (even within the same block/height), the new nonce baseline should be `max(initial_nonce_value(block_height), previous_nonce_of_deleted_key + 1)`, or the runtime should track a per-account/public-key "highest ever consumed nonce" independent of the access-key record's lifecycle, so deleting and re-adding a key can never roll the effective nonce backward.

### Proof of Concept
Runtime integration test plan (extending `runtime/runtime/src/tests/apply.rs` or `access_key_nonce_for_implicit_accounts.rs` style tests):
1. Set up an account at fixed block height `h` with an already-existing full-access key `K`, with `access_key.nonce` at some baseline `(h-1)*1e6`.
2. Apply `M` transactions signed by `K` with strictly increasing nonces up to `X` (all with `apply_state.block_height == h`), confirming `get_access_key(K).nonce == X` after commit.
3. In the same block height `h`, apply a transaction with actions `[Action::DeleteKey(K), Action::AddKey(K, same_pubkey)]`.
4. Assert `get_access_key(K).nonce == (h-1)*1e6` (i.e., strictly less than `X`), confirming the rollback.
5. Construct an old `SignedTransaction` using `K` with nonce `Y` where `(h-1)*1e6 < Y <= X` (a nonce already consumed in step 2) and assert it is accepted by `get_signer_and_access_key`/transaction verification (`ProcessTxResponse::ValidTx` / no `InvalidNonce` error), demonstrating the previously-spent nonce is valid again.

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

**File:** runtime/runtime/src/access_keys.rs (L136-147)
```rust
fn delete_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
) {
    let storage_usage = access_key_storage_usage(fee_config, public_key, access_key);
    remove_access_key(state_update, account_id.clone(), public_key.clone());
    account.set_storage_usage(account.storage_usage().saturating_sub(storage_usage));
}
```

**File:** runtime/runtime/src/access_keys.rs (L149-192)
```rust
pub(crate) fn action_add_key(
    apply_state: &ApplyState,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    add_key: &AddKeyAction,
) -> Result<(), StorageError> {
    if get_access_key(state_update, account_id, &add_key.public_key)?.is_some() {
        result.result = Err(ActionErrorKind::AddKeyAlreadyExists {
            account_id: account_id.to_owned(),
            public_key: add_key.public_key.clone().into(),
        }
        .into());
        return Ok(());
    }

    let fee_config = &apply_state.config.fees;

    if let Some(gas_key_info) = add_key.access_key.gas_key_info() {
        add_gas_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            gas_key_info,
            apply_state.block_height,
        )?;
    } else {
        add_regular_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            apply_state.block_height,
        )?;
    }

    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L230-241)
```rust
fn add_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    block_height: BlockHeight,
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
```

**File:** runtime/runtime/src/lib.rs (L691-712)
```rust
            Action::AddKey(add_key) => {
                metrics::ACTION_CALLED_COUNT.add_key.inc();
                action_add_key(
                    apply_state,
                    state_update,
                    account.as_mut().expect(EXPECT_ACCOUNT_EXISTS),
                    &mut result,
                    account_id,
                    add_key,
                )?;
            }
            Action::DeleteKey(delete_key) => {
                metrics::ACTION_CALLED_COUNT.delete_key.inc();
                action_delete_key(
                    &apply_state.config,
                    state_update,
                    account.as_mut().expect(EXPECT_ACCOUNT_EXISTS),
                    &mut result,
                    account_id,
                    delete_key,
                )?;
            }
```

**File:** runtime/runtime/src/actions.rs (L739-760)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** integration-tests/src/tests/features/access_key_nonce_for_implicit_accounts.rs (L112-194)
```rust
/// Helper for checking that duplicate transactions from NEAR-implicit accounts are properly rejected.
/// It creates NEAR-implicit account, deletes it and creates again, so that nonce of the access
/// key is updated. Then it tries to send tx from NEAR-implicit account with invalid nonce, which
/// should fail since the protocol upgrade.
fn get_status_of_tx_hash_collision_for_near_implicit_account(
    protocol_version: ProtocolVersion,
    near_implicit_account_signer: Signer,
) -> ProcessTxResponse {
    let epoch_length = 100;
    let mut genesis = Genesis::test(vec!["test0".parse().unwrap(), "test1".parse().unwrap()], 1);
    genesis.config.epoch_length = epoch_length;
    genesis.config.transaction_validity_period = epoch_length * 2;
    genesis.config.protocol_version = protocol_version;
    let mut env = TestEnv::builder(&genesis.config).nightshade_runtimes(&genesis).build();
    let genesis_block = env.clients[0].chain.get_block_by_height(0).unwrap();
    let deposit_for_account_creation = Balance::from_millinear(100);
    let mut height = 1;
    let blocks_number = 5;
    let signer1 = InMemorySigner::test_signer(&"test1".parse().unwrap());
    let near_implicit_account_id = near_implicit_account_signer.get_account_id();

    // Send money to NEAR-implicit account, invoking its creation.
    let send_money_tx = SignedTransaction::send_money(
        1,
        "test1".parse().unwrap(),
        near_implicit_account_id.clone(),
        &signer1,
        deposit_for_account_creation,
        *genesis_block.hash(),
    );
    height = check_tx_processing(&mut env, send_money_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Delete NEAR-implicit account.
    let delete_account_tx = SignedTransaction::delete_account(
        // Because AccessKeyNonceRange is enabled, correctness of this nonce is guaranteed.
        (height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER,
        near_implicit_account_id.clone(),
        near_implicit_account_id.clone(),
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        *block.hash(),
    );
    height = check_tx_processing(&mut env, delete_account_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Send money to NEAR-implicit account again, invoking its second creation.
    let send_money_again_tx = SignedTransaction::send_money(
        2,
        "test1".parse().unwrap(),
        near_implicit_account_id.clone(),
        &signer1,
        deposit_for_account_creation,
        *block.hash(),
    );
    height = check_tx_processing(&mut env, send_money_again_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Send money from NEAR-implicit account with incorrect nonce.
    let send_money_from_near_implicit_account_tx = SignedTransaction::send_money(
        1,
        near_implicit_account_id.clone(),
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        Balance::from_yoctonear(100),
        *block.hash(),
    );
    let response =
        env.rpc_handlers[0].process_tx(send_money_from_near_implicit_account_tx, false, false);

    // Check that sending money from NEAR-implicit account with correct nonce is still valid.
    let send_money_from_near_implicit_account_tx = SignedTransaction::send_money(
        (height - 1) * AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER,
        near_implicit_account_id,
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        Balance::from_yoctonear(100),
        *block.hash(),
    );
    check_tx_processing(&mut env, send_money_from_near_implicit_account_tx, height, blocks_number);

    response
}
```
