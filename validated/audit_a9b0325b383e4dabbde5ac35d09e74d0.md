This is exactly the analog I was looking for. This is a confirmed, already-fixed historical bug in nearcore that precisely mirrors the Mango "missing account check" vulnerability: a receipt/receiver identity that should have been validated against a derived/config value wasn't checked against the correct field, allowing an attacker to target a different account than intended.

### Title
Historical bug (already fixed): `validate_delegate_action` checked `DeterministicStateInit` receiver against the wrong id, allowing meta-transactions to target a mismatched deterministic account - ([File: runtime/runtime/src/action_validation.rs])

### Summary
Prior to the `FixDelegatedDeterministicStateInit` protocol feature, `validate_delegate_action` in `runtime/runtime/src/action_validation.rs` validated the inner `DeterministicStateInitAction`'s derived account id against the **outer transaction's `receiver_id`** instead of the **`DelegateAction`'s own `receiver_id`**. This is structurally the same class of bug as the Mango report: a security-critical account/address field (`utp_config.address` in Mango; the deterministic account's derived id in nearcore) that should be checked against the actual authorized target was instead checked against an unrelated/attacker-influenced value, letting the relayer/attacker substitute a different account than the one the signer authorized.

### Finding Description
`DeterministicStateInitAction` must only be applied to the one account whose id is cryptographically derived from its `state_init` payload (`derive_near_deterministic_account_id`), enforced by `validate_deterministic_state_init` comparing `derived_id` to `receiver_id` [1](#0-0) .

For a `DelegateAction` (meta-transaction), the inner actions are addressed to `delegate_action.receiver_id`, not the outer transaction's `receiver_id` (the outer tx's receiver is the delegate `sender_id`, per the meta-tx relay design) [2](#0-1) .

Before the fix, `validate_delegate_action` passed the *outer* `receiver` parameter (the tx's receiver, i.e., the sender/relay account) into `validate_actions_with_mode` for the inner actions instead of `delegate_action.receiver_id()`: [3](#0-2) 

This is exactly analogous to the Mango bug: the code trusted/derived identity from the wrong source of truth (outer tx receiver vs. the actual delegate target; Mango account address supplied by caller vs. `utp_config.address`) instead of validating the field that actually determines authorization/binding.

### Impact Explanation
The impact was scoped by defense-in-depth: this flawed check occurred only at **transaction admission/validation** time (`validate_delegate_action`, called from `validate_transaction`); when the relayer's transaction was later unpacked and converted into the actual receipt sent to `delegate_action.receiver_id`, `validate_receipt` independently re-validates `InvalidDeterministicStateInitReceiver` against the correct id [4](#0-3) 
so the exploit could pass initial validation but was still rejected at execution — the nearcore team's own comment states the bug "cannot be abused" [5](#0-4) , and dedicated regression tests confirm the pre-fix tx fails at receipt validation rather than executing incorrectly [6](#0-5) .

Because of this second independent check at receipt validation, there is no reachable path to theft, freezing of funds, or authorization escalation on the shipped/production code — this is a validated-and-fixed defense-in-depth gap, not an exploitable vulnerability in the current codebase (`FixDelegatedDeterministicStateInit` is included in the base protocol per `MIN_SUPPORTED_PROTOCOL_VERSION = 83`).

### Likelihood Explanation
Not applicable as a live vulnerability — the bug is already fixed and protocol-gated (`FixDelegatedDeterministicStateInit`), and even pre-fix it was non-exploitable due to the redundant `validate_receipt` check. This is reported purely as the closest structural analog to the reported bug class, per the requested "bug-class hint" framing, not as a currently exploitable path.

### Recommendation
No action needed on the current codebase — the fix is already merged and protocol-gated. This confirms the general pattern (validate identity/address fields against the specific authorized source-of-truth field, not an adjacent context field) is already correctly enforced with redundant checks (`action_validation.rs` at both tx-validation and receipt-validation stages) for this code path.

### Proof of Concept
The existing regression test demonstrates the pre-fix behavior and confirms it is caught before any state change: `test_deterministic_state_init_meta_tx_receiver_check_pre_fix` crafts a `DelegateAction` with `sender_id = det_account_b`, `receiver_id = det_account_a` (wrong target), wrapping a `DeterministicStateInitAction` for `state_init_b`; at protocol version `fix_version - 1` the tx passes initial validation but fails with `InvalidDeterministicStateInitReceiver` at `NewReceiptValidationError` [7](#0-6) , with the full exploit-transaction construction shown at [8](#0-7) .

### Citations

**File:** runtime/runtime/src/action_validation.rs (L182-220)
```rust
fn validate_delegate_action(
    limit_config: &LimitConfig,
    delegate_action: VersionedDelegateActionRef<'_>,
    receiver: &AccountId,
    current_protocol_version: ProtocolVersion,
    mode: ValidateReceiptMode,
) -> Result<(), ActionsValidationError> {
    // Check the count before `get_actions()` clones the list, so a huge
    // nested-action list can't force the allocation before being rejected.
    // Consensus-neutral: same `TotalNumberOfActionsExceeded` as the check in
    // `validate_actions_with_mode` below.
    let num_actions = delegate_action.actions().len() as u64;
    if num_actions > limit_config.max_actions_per_receipt {
        return Err(ActionsValidationError::TotalNumberOfActionsExceeded {
            total_number_of_actions: num_actions,
            limit: limit_config.max_actions_per_receipt,
        });
    }
    let actions = delegate_action.get_actions();
    let inner_receiver =
        if ProtocolFeature::FixDelegatedDeterministicStateInit.enabled(current_protocol_version) {
            // This is the correct receiver id to use for the check.
            delegate_action.receiver_id()
        } else {
            // This is a bug fixed with `FixDelegatedDeterministicStateInit` that
            // validated against the wrong id. This makes it impossible to
            // initialize deterministic accounts from meta transactions.
            // The bug cannot be abused, if someone crafts a state init that passes
            // validation here, it will fail when it is checked as incoming receipt.
            receiver
        };
    validate_actions_with_mode(
        limit_config,
        &actions,
        inner_receiver,
        current_protocol_version,
        mode,
    )?;
    Ok(())
```

**File:** runtime/runtime/src/action_validation.rs (L435-449)
```rust
fn validate_deterministic_state_init(
    limit_config: &LimitConfig,
    action: &DeterministicStateInitAction,
    receiver_id: &AccountId,
) -> Result<(), ActionsValidationError> {
    validate_global_contract_identifier(action.state_init.code())?;

    let derived_id = derive_near_deterministic_account_id(&action.state_init);

    if derived_id != *receiver_id {
        return Err(ActionsValidationError::InvalidDeterministicStateInitReceiver {
            derived_id,
            receiver_id: receiver_id.clone(),
        });
    }
```

**File:** docs/architecture/how/meta-tx.md (L40-52)
```markdown
With meta transactions, Alice can create a `DelegateAction`, which is very
similar to a transaction. It also contains a list of actions to execute and a
single receiver for those actions. She signs the `DelegateAction` and forwards
it (off-chain) to a relayer. The relayer wraps it in a transaction, of which the
relayer is the signer and therefore pays the gas costs. If the inner actions
have an attached token balance, this is also paid for by the relayer.

On chain, the `SignedDelegateAction` inside the transaction is converted to an
action receipt with the same `SignedDelegateAction` on the relayer's shard. The
receipt is forwarded to the account from `Alice`, which will unpacked the
`SignedDelegateAction` and verify that it is signed by Alice with a valid Nonce
etc. If all checks are successful, a new action receipt with the inner actions
as body is sent to `FT`. There, the `ft_transfer` call finally executes.
```

**File:** runtime/runtime/src/verifier.rs (L556-571)
```rust
    match receipt.versioned_receipt() {
        VersionedReceiptEnum::Action(action_receipt)
        | VersionedReceiptEnum::PromiseYield(action_receipt) => validate_action_receipt(
            limit_config,
            action_receipt,
            receipt.receiver_id(),
            current_protocol_version,
            mode,
        ),
        VersionedReceiptEnum::Data(data_receipt)
        | VersionedReceiptEnum::PromiseResume(data_receipt) => {
            validate_data_receipt(limit_config, &data_receipt)
        }
        VersionedReceiptEnum::GlobalContractDistribution(_) => Ok(()), // Distribution receipt can't be issued without a valid contract
    }
}
```

**File:** test-loop-tests/src/tests/deterministic_account_id.rs (L128-171)
```rust
/// Ensure there is no exploit with invalid deterministic account ids through
/// meta transactions.
///
/// With the old (buggy) code, `validate_delegate_action` used
/// `outer_tx.receiver_id` instead of `delegate_action.receiver_id` when
/// checking inner actions. The exploit tx therefore passes initial tx
/// validation. The exploit is prevented by a following `validate_receipt` check
/// when the meta transaction is unpacked.
#[test]
fn test_deterministic_state_init_meta_tx_receiver_check_pre_fix() {
    let fix_version = ProtocolFeature::FixDelegatedDeterministicStateInit.protocol_version();
    let outcome = try_meta_tx_deterministic_receiver_exploit(fix_version - 1)
        .expect("without the fix, exploit tx passes initial tx validation");

    assert_matches!(
        outcome.status,
        FinalExecutionStatus::Failure(TxExecutionError::ActionError(ActionError {
            kind: ActionErrorKind::NewReceiptValidationError(
                ReceiptValidationError::ActionsValidation(
                    ActionsValidationError::InvalidDeterministicStateInitReceiver { .. }
                )
            ),
            ..
        })),
        "expected InvalidDeterministicStateInitReceiver in NewReceiptValidationError, got: {:?}",
        outcome.status
    );
}

/// With `FixDelegatedDeterministicStateInit` in place, the exploit should
/// already be caught at the first tx validation.
#[test]
fn test_deterministic_state_init_meta_tx_receiver_check() {
    let fix_version = ProtocolFeature::FixDelegatedDeterministicStateInit.protocol_version();
    let err = try_meta_tx_deterministic_receiver_exploit(fix_version)
        .expect_err("exploit tx must be rejected at tx validation with the fix");
    assert_matches!(
        err,
        InvalidTxError::ActionsValidation(
            ActionsValidationError::InvalidDeterministicStateInitReceiver { .. }
        ),
        "wrong error: {err:?}"
    );
}
```

**File:** test-loop-tests/src/tests/deterministic_account_id.rs (L179-262)
```rust
fn try_meta_tx_deterministic_receiver_exploit(
    protocol_version: ProtocolVersion,
) -> Result<FinalExecutionOutcomeView, InvalidTxError> {
    let mut env = TestEnv::setup_with_version(Balance::from_near(100), protocol_version);
    env.deploy_global_contract(GlobalContractDeployMode::AccountId);

    let (_state_init_a, det_account_a) = env.new_deterministic_account_with_data(small());
    let (state_init_b, det_account_b) = env.new_deterministic_account_with_data(big());
    assert_ne!(det_account_a, det_account_b);

    // Deploy det_account_b and add a full-access key so it can act as meta_tx_sender.
    let user_signer = create_user_test_signer(&env.user_account());
    let storage_balance = env.balance_for_storage(state_init_b.clone());
    let deploy_tx = SignedTransaction::deterministic_state_init(
        env.next_nonce(),
        env.user_account(),
        det_account_b.clone(),
        &user_signer,
        env.get_tx_block_hash(),
        state_init_b.clone(),
        storage_balance,
    );
    env.run_tx(deploy_tx);

    let meta_tx_sender_signer = create_user_test_signer(&det_account_b);
    let pk_base64 = near_primitives_core::serialize::to_base64(
        &borsh::to_vec(&meta_tx_sender_signer.public_key()).unwrap(),
    );
    let add_key_args = serde_json::json!([
        { "batch_create": { "account_id": det_account_b.as_str() }, "id": 0 },
        {
            "action_add_key_with_full_access": {
                "promise_index": 0,
                "public_key": pk_base64,
                "nonce": 0
            },
            "id": 0,
            "return": true
        }
    ]);
    let add_key_tx = SignedTransaction::call(
        env.next_nonce(),
        env.user_account(),
        det_account_b.clone(),
        &user_signer,
        Balance::from_near(2),
        "call_promise".to_owned(),
        serde_json::to_vec(&add_key_args).unwrap(),
        Gas::from_teragas(300),
        env.get_tx_block_hash(),
    );
    env.run_tx(add_key_tx);

    // Craft the exploit: outer_tx.receiver = det_account_b = derive(state_init_b).
    // Old check: det_account_b == derive(state_init_b) passes.
    // The delegate action targets det_account_a, which is the wrong account.
    // In no protocol version can this ever be allowed to be executed successfully.
    let relayer = env.independent_account();
    let relayer_signer = create_user_test_signer(&relayer);
    let inner_action = Action::DeterministicStateInit(Box::new(DeterministicStateInitAction {
        state_init: state_init_b,
        deposit: Balance::ZERO,
    }));
    let delegate_nonce = env.next_nonce_for(&det_account_b);
    let delegate_action = DelegateAction {
        sender_id: det_account_b.clone(),
        receiver_id: det_account_a,
        actions: vec![NonDelegateAction::try_from(inner_action).unwrap()],
        nonce: delegate_nonce,
        max_block_height: 1_000_000,
        public_key: meta_tx_sender_signer.public_key(),
    };
    let signed_delegate_action =
        SignedDelegateAction::sign(&meta_tx_sender_signer, delegate_action);
    let tx = SignedTransaction::from_actions(
        env.next_nonce(),
        relayer,
        det_account_b,
        &relayer_signer,
        vec![Action::Delegate(Box::new(signed_delegate_action))],
        env.get_tx_block_hash(),
    );
    env.try_execute_tx(tx)
}
```
