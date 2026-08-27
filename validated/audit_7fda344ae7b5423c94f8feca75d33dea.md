Confirmed by `test_apply_v4_metadata_pads_unexecuted_actions`: when an action within a receipt fails (e.g. `DeleteKeyDoesNotExist`), the action loop `break`s and no subsequent actions in the same receipt execute [1](#0-0) , confirmed by the test at [2](#0-1) . This confirms the wallet-contract freeze scenario below is real: if `delete_key` fails inside `create_ban_relayer_promise`'s single-receipt batch, the trailing `ban_relayer` call is never executed.

### Title
Wallet Contract permanently freezes ETH-implicit account funds when the "ban relayer" batch's `DeleteKey` action fails, since no privileged party can ever reset `has_in_flight_tx` - (File: runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs)

### Summary
The NEAR Wallet Contract (NEP-518), deployed on every ETH-implicit account, enforces a "one in-flight transaction at a time" invariant via the `has_in_flight_tx` flag. This flag is only ever cleared inside one of the contract's own private callbacks (`address_check_callback`, `nep_141_storage_balance_callback`, `rlp_execute_callback`, `ban_relayer`). When a submitted relayer transaction is judged faulty, `rlp_execute` builds a promise that (1) deletes the offending relayer's access key and (2) calls `ban_relayer` to reset the flag, both as actions in a single batched receipt with no `.then()` in between [3](#0-2) [4](#0-3) . Because NEAR aborts the remaining actions in a receipt as soon as one action fails [5](#0-4) , if the `DeleteKey` action fails (e.g. `DeleteKeyDoesNotExist` because the key was already removed by an earlier ban, or is otherwise absent), `ban_relayer` never runs and `has_in_flight_tx` is permanently stuck at `true`.

### Finding Description
`WalletContract::rlp_execute` is the sole entry point for interacting with an ETH-implicit account; it rejects any call while `has_in_flight_tx` is `true` [6](#0-5) . When `inner_rlp_execute` returns `Error::Relayer(_)` and the caller (`env::signer_account_id`) equals the wallet's own account (i.e. the transaction was signed with a `FunctionCall` access key previously granted to a relayer, as documented in the "register relayer" flow [7](#0-6) ), the contract responds by sending `create_ban_relayer_promise`, which does `delete_key(pk).function_call_weight("ban_relayer", ...)` — two actions batched onto the same promise/receipt, not chained via `.then()` [4](#0-3) .

Per the runtime's action-execution semantics, actions inside one action receipt execute sequentially, and the loop breaks on the first failure, so no later action in that receipt runs [8](#0-7) ; this is the exact mechanism validated by `test_apply_v4_metadata_pads_unexecuted_actions`, where a failing `DeleteKey` at index 1 prevents the subsequent `FunctionCall` at index 2 from ever executing [9](#0-8) .

If the relayer's public key referenced by `env::signer_account_pk()` has already been deleted (e.g. from a prior faulty-relayer ban, or any other cause of key removal before this receipt executes — race conditions across chunks, retried relayer submissions with stale key state, or a user/relayer deleting the key themselves mid-flight), `delete_key` fails with `DeleteKeyDoesNotExist`, the whole receipt fails, `ban_relayer` is skipped, and `has_in_flight_tx` remains `true` forever. Since `rlp_execute` is the only way to interact with the account and it unconditionally refuses to proceed while `has_in_flight_tx` is `true` [10](#0-9) , and ETH-implicit accounts can never be given a full-access key nor be deleted [11](#0-10) , there is no on-chain recovery path once this state is reached — no governance-equivalent, no owner override, nothing.

This is directly analogous to the Yaxis finding: an action gated by a permission model meant to bound damage to an unprivileged/lower-trust actor (the relayer, who is explicitly documented as untrusted and subject to being banned) can instead trigger a state transition that halts the protected resource (the wallet) permanently, with no privileged recovery mechanism, because the "ban" mechanism's own multi-action side effect is not atomic/robust against its own precondition failing.

### Impact Explanation
Once `has_in_flight_tx` is stuck `true`, the ETH-implicit account's Wallet Contract can never process another transaction: no `Transfer`, `FunctionCall`, `AddKey`, or `DeleteKey` action can be relayed for that account ever again. Since ETH-implicit accounts have no other access mechanism (no full-access key can be added and the account cannot be deleted), all $NEAR and any other funds attached to that specific account become permanently frozen. This matches "permanent freezing of funds" from an unprivileged actor's action, without any theft, but with total, irreversible loss of usability of the wallet.

### Likelihood Explanation
Triggering requires: (a) a relayer key registered via `AddKey` with `FunctionCallPermission` to `rlp_execute` (a normal, documented, intended usage pattern for this contract — see `register_relayer`/`test_register_relayer` [12](#0-11) ), and (b) that relayer's key to no longer exist at the time a "ban" is attempted against it — a state reachable simply by the relayer submitting two faulty transactions (the first ban removes the key and succeeds; if a second faulty-transaction receipt referencing the same now-removed key is already in flight, delayed, or resubmitted before its own ban resolves) or by any legitimate `DeleteKey` racing with a pending faulty-tx receipt. Given the flag is asserted as a strict invariant by the code's own comments ("has_in_flight_tx must be true when a mutable method... returns a promise and false otherwise") [13](#0-12) , but the contract does not verify the relayer key still exists before attempting to delete it, the precondition for the DoS is plausible under ordinary operational conditions with an untrusted/malfunctioning relayer, without requiring any node-level or consensus-level compromise.

### Recommendation
Do not couple `delete_key` and the `ban_relayer` callback in a single non-chained batch. Instead, chain them with `.then()` so `ban_relayer` (or an equivalent flag-reset) executes regardless of whether the `delete_key` action succeeds, or have `ban_relayer` be invoked via a callback that inspects `promise_result` rather than depending on the preceding action in the same receipt succeeding. At minimum, ensure `has_in_flight_tx` is reset even when the key-deletion action fails, e.g. by making the reset the first guaranteed step of a `.then()`-chained follow-up call rather than a batched sibling action.

### Proof of Concept
1. Register a relayer key on an ETH-implicit account's Wallet Contract via `AddKey` with `FunctionCallPermission { receiver_id: <wallet>, method_names: ["rlp_execute"] }`, per the documented relayer-registration flow [14](#0-13) .
2. Have the relayer submit a faulty RLP transaction that resolves to `Error::Relayer(_)` while `env::signer_account_id() == current_account_id`, triggering `create_ban_relayer_promise` which schedules `delete_key(relayer_pk)` + `ban_relayer` in one un-chained batch [4](#0-3) .
3. Before/concurrently, ensure the relayer's key is already removed at execution time (e.g., by submitting two faulty transactions using the same key such that the first ban's `delete_key` commits before the second ban-attempt's `delete_key` executes, or by the relayer/owner independently deleting the key while a faulty-tx receipt referencing it is still in flight).
4. The second `delete_key` action fails with `DeleteKeyDoesNotExist`; per the confirmed runtime semantics, `ban_relayer` is never invoked, so `has_in_flight_tx` remains `true`.
5. Any subsequent `rlp_execute` call against this wallet is rejected with "transaction already in progress, please try again later" forever, permanently freezing the account.

### Citations

**File:** runtime/runtime/src/lib.rs (L891-950)
```rust
            // Executing actions one by one
            for (action_index, action) in action_receipt.actions().iter().enumerate() {
                let action_hash = create_action_hash_from_receipt_id(
                    receipt.receipt_id(),
                    apply_state.block_height,
                    action_index,
                );
                let mut new_result = self.apply_action(
                    action,
                    state_update,
                    apply_state,
                    preparation_pipeline,
                    &mut account,
                    &mut actor_id,
                    receipt,
                    &action_receipt,
                    Arc::clone(&promise_results),
                    &action_hash,
                    action_index,
                    &action_receipt.actions(),
                    epoch_info_provider,
                    storage_proof_size_before_receipt,
                )?;
                if new_result.result.is_ok() {
                    if let Err(e) = new_result.new_receipts.iter().try_for_each(|receipt| {
                        validate_receipt(
                            &apply_state.config.wasm_config.limit_config,
                            receipt,
                            apply_state.current_protocol_version,
                            ValidateReceiptMode::NewReceipt,
                        )
                    }) {
                        new_result.result =
                            Err(ActionErrorKind::NewReceiptValidationError(e).into());
                    }
                }
                result.merge(new_result)?;
                if let (true, Some(size_before), Some(limit)) = (
                    result.result.is_ok(),
                    storage_proof_size_before_receipt,
                    storage_proof_limit_for_all_actions,
                ) {
                    let recorded_by_receipt = state_update
                        .trie
                        .recorded_storage_size_upper_bound()
                        .saturating_sub(size_before);
                    if recorded_by_receipt > limit {
                        result.set_error(
                            ActionErrorKind::ReceiptStorageProofSizeExceeded {
                                limit: limit as u64,
                            }
                            .into(),
                        );
                    }
                }
                // TODO storage error
                if let Err(ref mut res) = result.result {
                    res.index = Some(action_index as u64);
                    break;
                }
```

**File:** runtime/runtime/src/tests/apply.rs (L3760-3820)
```rust
/// When a non-final action errors, the action loop breaks before later
/// actions run. The V4 `contracts` vector is then resized to match the
/// receipt's action count with `AccountContract::None`, so consumers can
/// still index by action position. Here the receipt is
/// [DeployContract, DeleteKey(missing), FunctionCall]: action 0 deploys
/// rs_contract (pre-action contract: `None`), action 1 then fails (pre-action
/// contract: `Local(rs_hash)` — the deploy from action 0 took effect even
/// though the receipt as a whole fails), and the trailing FunctionCall never
/// runs — its slot must land on `None` via the resize pad, not via a real
/// contract resolution. The `Local(rs_hash)` entry in the middle is what
/// distinguishes a real per-action capture from the pad.
#[test]
fn test_apply_v4_metadata_pads_unexecuted_actions() {
    let (runtime, tries, root, apply_state, signers, epoch_info_provider) = setup_runtime(
        vec![alice_account()],
        Balance::from_near(1_000_000),
        Balance::from_near(500_000),
        Gas::from_teragas(1000),
    );

    let nonexistent_pk =
        InMemorySigner::from_seed(alice_account(), KeyType::ED25519, "nonexistent").public_key();
    let receipt = create_receipt_with_actions(
        alice_account(),
        signers[0].clone(),
        vec![
            Action::DeployContract(DeployContractAction {
                code: near_test_contracts::rs_contract().to_vec(),
            }),
            Action::DeleteKey(Box::new(DeleteKeyAction { public_key: nonexistent_pk })),
            Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "log_something".to_string(),
                args: vec![],
                gas: MAX_ATTACHED_GAS.checked_div(2).unwrap(),
                deposit: Balance::ZERO,
            })),
        ],
    );

    let apply_result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[receipt],
            SignedValidPeriodTransactions::empty(),
            &epoch_info_provider,
            Default::default(),
        )
        .unwrap();

    let outcome = assert_matches!(
        &apply_result.outcomes[..],
        [ExecutionOutcomeWithId { id: _, outcome }] => outcome
    );
    let action_error = assert_matches!(
        &outcome.status,
        ExecutionStatus::Failure(TxExecutionError::ActionError(ae)) => ae
    );
    assert_eq!(action_error.index, Some(1));
    assert_matches!(action_error.kind, ActionErrorKind::DeleteKeyDoesNotExist { .. });
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L46-55)
```rust
pub struct WalletContract {
    pub nonce: u64,
    /// Tracks whether a transaction is currently being executed
    /// (i.e. has receipts that have not yet resolved).
    /// Invariant: `has_in_flight_tx` must be `true` when a mutable method
    /// of this contract returns a promise and `false` otherwise (except
    /// for the check if a transaction is already in flight at the beginning
    /// of `rlp_execute`).
    pub has_in_flight_tx: bool,
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L93-128)
```rust
    ) -> PromiseOrValue<ExecuteResponse> {
        // To ensure user actions are executed in the desired order,
        // having multiple transactions in flight at the same time is
        // not allowed.
        if self.has_in_flight_tx {
            return PromiseOrValue::Value(ExecuteResponse {
                success: false,
                success_value: None,
                error: Some(
                    "Error: transaction already in progress, please try again later.".into(),
                ),
            });
        }
        let current_account_id = env::current_account_id();
        let predecessor_account_id = env::predecessor_account_id();
        let result = inner_rlp_execute(
            current_account_id.clone(),
            predecessor_account_id,
            target,
            tx_bytes_b64,
            &mut self.nonce,
        );

        match result {
            Ok(promise) => {
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(Error::Relayer(_)) if env::signer_account_id() == current_account_id => {
                let promise = create_ban_relayer_promise(current_account_id);
                self.has_in_flight_tx = true;
                PromiseOrValue::Promise(promise)
            }
            Err(e) => PromiseOrValue::Value(e.into()),
        }
    }
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L503-512)
```rust
fn create_ban_relayer_promise(current_account_id: AccountId) -> Promise {
    let pk = env::signer_account_pk();
    Promise::new(current_account_id).delete_key(pk).function_call_weight(
        "ban_relayer".into(),
        Vec::new(),
        NearToken::from_yoctonear(0),
        Gas::from_tgas(1),
        GasWeight(1),
    )
}
```

**File:** docs/DataStructures/Account.md (L119-122)
```markdown
Once a NEAR-implicit account is created it acts as a regular account until it's deleted.

An ETH-implicit account can only be used by calling the methods of the [Wallet Contract](#wallet-contract). It cannot be deleted, nor can a full access key be added.
The primary purpose of ETH-implicit accounts is to enable seamless integration of existing Ethereum tools (such as wallets) with the NEAR blockchain.
```

**File:** docs/DataStructures/Account.md (L124-136)
```markdown
### Wallet Contract

The Wallet Contract (see [NEP-518](https://github.com/near/NEPs/issues/518) for more details) functions as a user account and is designed to receive, validate, and execute Ethereum-compatible transactions on the NEAR blockchain.

Without going into details, an Ethereum-compatible wallet user sends a transaction to an RPC endpoint, which wraps it and passes it to the Wallet Contract (on the target account) as an `rlp_execute(target: AccountId, tx_bytes_b64: Vec<u8>)` contract call.
Then, the contract parses `tx_bytes_b64` and verifies it is signed with the private key matching the target [ETH-implicit account ID](#eth-implicit-account-id) on which the contract is hosted.

Under the hood, the transaction encodes a NEAR-native action. Currently supported actions are:

- Transfer (from ETH-implicit account).
- Function call (call another contract).
- Add `AccessKey` with `FunctionCallPermission`. This allows adding a relayer's public key to an ETH-implicit account, enabling the relayer to pay the gas fee for transactions from this account. Still, each transaction has to be signed by the owner of the account (corresponding Secp256K1 private key).
- Delete `AccessKey`.
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/relayer.rs (L21-52)
```rust
// A relayer can use its own Near account to send a transaction containing data
// signed by the user which adds a FunctionCall access key to the Wallet
// Contract account. This allows the relayer to send transactions on the user's
// behalf while the user covers the gas costs.
#[tokio::test]
async fn test_register_relayer() -> anyhow::Result<()> {
    let TestContext { worker, mut wallet_contract, wallet_sk, .. } = TestContext::new().await?;

    let relayer_pk = wallet_contract.register_relayer(&worker).await?;
    let key = wallet_contract.inner.as_account().view_access_key(&relayer_pk).await?;
    match &key.permission {
        AccessKeyPermission::FunctionCall(access) => {
            assert_eq!(access.allowance, None);
            assert_eq!(access.receiver_id.as_str(), wallet_contract.inner.id().as_str());
            assert_eq!(&access.method_names, &[RLP_EXECUTE]);
        }
        _ => panic!("Unexpected full access key"),
    }

    // Should be able to submit transactions using the new key
    utils::deploy_and_call_hello(&worker, &wallet_contract, &wallet_sk, 1).await?;

    // If the relayer is dishonest then its key is revoked.
    // In this case the relayer will try to repeat a nonce value.
    let result = utils::deploy_and_call_hello(&worker, &wallet_contract, &wallet_sk, 1).await;
    let error_message = format!("{:?}", result.unwrap_err());
    assert!(error_message.contains("faulty relayer"));

    assert_revoked_key(&wallet_contract.inner, &relayer_pk).await;

    Ok(())
}
```

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/tests/utils/test_context.rs (L92-137)
```rust
    /// Add a new `FunctionCall` access key to the Wallet Contract.
    /// The idea is that this allows the relayer to submit transactions signed by
    /// the Wallet Contract directly.
    pub async fn register_relayer(
        &mut self,
        worker: &Worker<Sandbox>,
    ) -> anyhow::Result<PublicKey> {
        let relayer_account = worker.dev_create_account().await?;
        let relayer_key = SecretKey::from_random(KeyType::ED25519);
        let relayer_pk = relayer_key.public_key();

        let action = Action::AddKey {
            public_key_kind: 0,
            public_key: relayer_pk.key_data().to_vec(),
            nonce: 0,
            is_full_access: false,
            is_limited_allowance: false,
            allowance: 0,
            receiver_id: self.inner.id().to_string(),
            method_names: vec![RLP_EXECUTE.into()],
        };
        let nonce = self.get_nonce().await?;
        let signed_transaction =
            utils::create_signed_transaction(nonce, self.inner.id(), Wei::zero(), action, &self.sk);

        // Call the Wallet Contract from the relayer account to add the key
        let result: ExecuteResponse = relayer_account
            .call(self.inner.id(), RLP_EXECUTE)
            .args_json(serde_json::json!({
                "target": self.inner.id(),
                "tx_bytes_b64": codec::encode_b64(&codec::rlp_encode(&signed_transaction))
            }))
            .max_gas()
            .transact()
            .await?
            .into_result()?
            .json()?;

        assert!(result.success, "Adding Relayer's key failed: {:?}", result.error);

        // Tell near-workspaces to use this new key instead when
        // signing transactions from the Wallet Contract
        self.inner.as_account_mut().set_secret_key(relayer_key);

        Ok(relayer_pk)
    }
```
