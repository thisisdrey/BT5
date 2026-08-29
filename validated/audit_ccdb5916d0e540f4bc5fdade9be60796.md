No vulnerability found for this question.

**Rationale:**

The exact scenario described — a receipt to account `A` sitting in the delayed/buffered queue while `A` is deleted and recreated before the receipt drains — is explicitly covered by an existing regression test, `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`, in `runtime/runtime/src/tests/apply.rs`. [1](#0-0) 

This confirms the runtime's actual (and intended) semantics: `apply_action_receipt` always does a fresh `get_account(state_update, account_id)` lookup at the moment the receipt is executed, not at the moment it was sent/buffered. [2](#0-1) 

There is no "authorization exactness" invariant in NEAR's receipt model that says a receipt must execute against the exact account state that existed when the predecessor sent it. Receipts are asynchronous by design — an account's state (balance, keys, contract code) can change between when a receipt is sent and when it executes, and the protocol always applies actions against whatever state exists at execution time. The buffered/delayed queue and congestion control only affect *when* a receipt executes, not what authorization guarantees it carries. [3](#0-2) 

Additionally, the claimed impact — "granting owner-level FunctionCall execution against the new account" — mischaracterizes `check_actor_permissions`. `FunctionCall`, `Transfer`, `CreateAccount`, and `TransferToGasKey` actions have **no** actor-identity restriction at all; only `DeleteAccount`, `AddKey`, `DeleteKey`, and `Stake` require `actor_id == account_id`. [4](#0-3) 

Since `FunctionCall` was never gated on `predecessor_id == receiver_id` or on any prior authorization from the current account owner, a receipt landing on a newly recreated (empty, no-code) account does not "escalate" any privilege — it simply executes a normal function call against whatever code (or lack thereof) currently exists, exactly as the test demonstrates (the call fails with `CompilationError::CodeDoesNotExist` because the new account has no contract deployed). This is standard, already-tested behavior, not a security boundary violation, and does not lead to theft/freezing of funds, consensus divergence, or a shard halt.

### Citations

**File:** runtime/runtime/src/tests/apply.rs (L4877-4982)
```rust
// A FunctionCall whose receiver is deleted and recreated within the same chunk must
// resolve to the freshly recreated (no-code) account, not to a stale contract that
// `ReceiptPreparationPipeline` compiled against the receiver's code as resolved at
// preparation time.
#[test]
fn test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code() {
    let parent = alice_account();
    let child: AccountId = "child.alice.near".parse().unwrap();
    // initial_locked must be 0 so the self-DeleteAccount receipt below passes the
    // DeleteAccountStaking check in `check_actor_permissions`.
    let (runtime, tries, root, mut apply_state, signers, epoch_info_provider) = setup_runtime(
        vec![parent.clone(), child.clone()],
        Balance::from_near(1_000_000),
        Balance::ZERO,
        Gas::from_teragas(1000),
    );
    let parent_signer = signers[0].clone();
    let child_signer = signers[1].clone();

    let deploy = create_receipt_with_actions(
        child.clone(),
        child_signer.clone(),
        vec![Action::DeployContract(DeployContractAction {
            code: near_test_contracts::trivial_contract().to_vec(),
        })],
    );
    let deploy_result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[deploy],
            SignedValidPeriodTransactions::empty(),
            &epoch_info_provider,
            Default::default(),
        )
        .unwrap();
    let root =
        commit_apply_result(&deploy_result, &mut apply_state, &tries, ShardUId::single_shard());
    apply_state.block_height += 1;

    let receipt_gas_price = GAS_PRICE.max(apply_state.config.min_gas_purchase_price);
    let build_receipt = |tag: &str, predecessor: AccountId, signer: &Signer, actions| -> Receipt {
        Receipt::V0(ReceiptV0 {
            predecessor_id: predecessor.clone(),
            receiver_id: child.clone(),
            receipt_id: CryptoHash::hash_borsh((tag, &child)),
            receipt: ReceiptEnum::Action(ActionReceipt {
                signer_id: predecessor,
                signer_public_key: signer.public_key(),
                gas_price: receipt_gas_price,
                output_data_receivers: vec![],
                input_data_ids: vec![],
                actions,
            }),
        })
    };
    let delete = build_receipt(
        "delete",
        child.clone(),
        &child_signer,
        vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id: parent.clone() })],
    );
    let create_and_call = build_receipt(
        "create_and_call",
        parent,
        &parent_signer,
        vec![
            Action::CreateAccount(CreateAccountAction {}),
            Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "main".to_string(),
                args: vec![],
                gas: Gas::from_teragas(10),
                deposit: Balance::ZERO,
            })),
        ],
    );
    let call_id = *create_and_call.receipt_id();

    let result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[delete, create_and_call],
            SignedValidPeriodTransactions::empty(),
            &epoch_info_provider,
            Default::default(),
        )
        .unwrap();

    let call_outcome = result
        .outcomes
        .iter()
        .find(|outcome| outcome.id == call_id)
        .expect("function call outcome missing");
    assert_matches!(
        &call_outcome.outcome.status,
        ExecutionStatus::Failure(TxExecutionError::ActionError(ActionError {
            kind: ActionErrorKind::FunctionCallError(FunctionCallError::CompilationError(
                CompilationError::CodeDoesNotExist { .. }
            )),
            ..
        }))
    );
}
```

**File:** runtime/runtime/src/lib.rs (L713-726)
```rust
            Action::DeleteAccount(delete_account) => {
                metrics::ACTION_CALLED_COUNT.delete_account.inc();
                action_delete_account(
                    state_update,
                    account,
                    actor_id,
                    receipt,
                    &mut result,
                    account_id,
                    delete_account,
                    &apply_state.config,
                    apply_state.current_protocol_version,
                )?;
            }
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/actions.rs (L760-783)
```rust
        }
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
        Action::CreateAccount(_)
        | Action::FunctionCall(_)
        | Action::Transfer(_)
        | Action::TransferToGasKey(_) => (),
        Action::Delegate(_) | Action::DelegateV2(_) => (),
        Action::DeterministicStateInit(_) => (),
    };
```
