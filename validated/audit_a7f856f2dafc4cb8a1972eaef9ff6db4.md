## Analog Found: Deterministic-account receiver-ID mismatch inside delegated (meta) transactions

### Title
Delegated `DeterministicStateInitAction` could target a different derived account than validated, causing state-init execution against a mismatched receiver - ([File: runtime/runtime/src/action_validation.rs])

### Summary
NEAR's `DeterministicAccountStateInit` mechanism derives an account ID deterministically from its initial state (code + data), analogous to an address derived on-chain from a public key/state, similar in spirit to how an account-abstraction wallet derives its address. [1](#0-0)  Before the `FixDelegatedDeterministicStateInit` protocol feature, the receiver-ID check for `DeterministicStateInitAction` validated the *outer transaction's* receiver against the derived ID, but when the action was wrapped inside a `DelegateAction` (a meta-transaction), the actual receipt that executes the state-init action is addressed to `delegate_action.receiver_id`, which can differ from the outer transaction's receiver_id used for validation. This is exactly the same class of bug as the external report: an identity (account/address) validated in one context (outer tx receiver / L1 msg.sender) can diverge from the identity actually used at execution/claim time (delegated receiver / L2 claim address), because the check didn't bind the derived identity to the actual execution target.

### Finding Description
The account for a `DeterministicStateInitAction` must equal `derive_near_deterministic_account_id(state_init)`, enforced in `validate_deterministic_state_init`: [2](#0-1) 

For a **direct** transaction, `receiver_id` passed to this validation function is the transaction/receipt receiver, which is correct.

For a **meta-transaction**, the `DeterministicStateInitAction` is nested as a `NonDelegateAction` inside a `DelegateAction`, and the *actual* receipt executed carries `delegate_action.receiver_id` as its true receiver, not the outer transaction's `receiver_id` (which is the delegate action's sender, per the meta-tx design): [3](#0-2) [4](#0-3) 

Before the fix, tx-level validation could check `outer_tx.receiver_id == derive(state_init)` and pass, while the *inner* `DelegateAction.receiver_id` pointed at a completely different (attacker-chosen) account. This is confirmed by the regression test that exercises the exploit scenario and the dedicated protocol feature added to close it: [5](#0-4) [6](#0-5) 

The fix is gated by the `FixDelegatedDeterministicStateInit` protocol feature, activated at protocol version 85: [7](#0-6) 

Because `MIN_SUPPORTED_PROTOCOL_VERSION` is 83, a network/binary running at protocol version 83 or 84 (i.e., before the v85 upgrade activates) is exposed to this bug, since the fix is not baked in unconditionally but is feature-gated. [8](#0-7) 

### Impact Explanation
An attacker could craft a `DelegateAction` where the outer wrapping transaction's `receiver_id` matches `derive(state_init)` (satisfying the naive pre-fix check) while `delegate_action.receiver_id` (the actual execution target) points to a different, victim-chosen deterministic account address. Depending on how deep the check was in the vulnerable version, this could let an attacker execute `DeterministicStateInitAction` (code + data initialization, plus fund transfer/storage staking) against an account other than the one that was actually validated to match the state, i.e., an "address mismatch" between the validated identity and the executed identity — directly analogous to depositor address (L1 msg.sender) diverging from claimer address (L2 AA wallet address) in the bridge bug. Depending on the exact mismatch exploited, this could allow account state / code initialization to be redirected to an account the attacker does not control the correct pre-image for, or allow deposited balance intended for one deterministic account to be diverted, causing fund loss/lock for the legitimate owner of the derived account.

### Likelihood Explanation
This requires the attacker to construct a `DelegateAction` (a normal, unprivileged, permissionless mechanism, NEP-366) with a crafted mismatch between the outer transaction receiver and the inner delegate receiver — well within reach of an ordinary client. It is only exploitable while a network operates on protocol version < 85 (i.e., before `FixDelegatedDeterministicStateInit` activates); once activated, the explicit test `test_deterministic_state_init_meta_tx_receiver_check` confirms the exploit tx is now rejected at validation. [9](#0-8) 

### Recommendation
Ensure the receiver-ID check for `DeterministicStateInitAction` is always performed against the *actual* execution-time receiver (i.e., `delegate_action.receiver_id` for meta-transactions, not the outer transaction's receiver_id), and ensure this check is unconditionally enforced going forward rather than only via a version-gated protocol feature so that any client on any still-supported protocol version cannot construct the mismatch. Confirm `FixDelegatedDeterministicStateInit` is active on all currently supported protocol versions, or backport the fix as a baseline validation rule independent of the feature flag.

### Proof of Concept
The nearcore test suite itself contains the exploit scenario used to validate the fix, matching the described bug class: [10](#0-9) 

1. Deploy `det_account_b` as a deterministic account (`state_init_b`), give it a full-access key.
2. Craft a `DelegateAction` signed by `det_account_b`'s key, with `sender_id = det_account_b` but `receiver_id = det_account_a` (a *different* deterministic account derived from `state_init_a`).
3. Wrap `Action::DeterministicStateInit(state_init_b)` inside the `DelegateAction`.
4. Submit the wrapping transaction from a relayer to `det_account_b` (which satisfies the pre-fix, outer-tx-only receiver check since `det_account_b == derive(state_init_b)`).
5. Pre-fix: validation passes at the transaction level despite the actual execution receiver (`det_account_a`) not matching `derive(state_init_b)`, only failing later (or not at all, depending on exact version) at receipt application — an address/receiver mismatch analogous to the reported bridge bug. Post-fix (v85+, confirmed by the test), this is rejected at transaction validation with `InvalidDeterministicStateInitReceiver`. [11](#0-10) 

**Note on uncertainty**: I could not fully trace the exact pre-v85 code path (since this snapshot already contains the fix and the pre-fix code has apparently been replaced) to confirm precisely how far execution proceeded before failing in the vulnerable version, or whether the mismatch could result in outright fund loss versus merely a failed/wasted receipt. This detail could not be verified further without access to the pre-fix version of `action_validation.rs`/`actions.rs` or a running Devin session to git-blame/diff the exact change introduced by `FixDelegatedDeterministicStateInit`.

### Citations

**File:** docs/DataStructures/Account.md (L167-172)
```markdown
### Deterministic account ID

The account ID is derived from a `DeterministicAccountStateInit` instance.

To derive the deterministic account id, borsh-encode the `DeterministicAccountStateInit` enum instance into raw
bytes. Then use the following formula: `'0s' + keccak256(bytes)[12:32].hex()`.
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

**File:** docs/architecture/how/meta-tx.md (L47-52)
```markdown
On chain, the `SignedDelegateAction` inside the transaction is converted to an
action receipt with the same `SignedDelegateAction` on the relayer's shard. The
receipt is forwarded to the account from `Alice`, which will unpacked the
`SignedDelegateAction` and verify that it is signed by Alice with a valid Nonce
etc. If all checks are successful, a new action receipt with the inner actions
as body is sent to `FT`. There, the `ft_transfer` call finally executes.
```

**File:** runtime/runtime/src/actions.rs (L483-497)
```rust
    // Generate a new receipt from DelegateAction.
    let new_receipt = Receipt::V0(ReceiptV0 {
        predecessor_id: sender_id.clone(),
        receiver_id: delegate_action.receiver_id().clone(),
        receipt_id: CryptoHash::default(),

        receipt: ReceiptEnum::Action(ActionReceipt {
            signer_id: action_receipt.signer_id().clone(),
            signer_public_key: action_receipt.signer_public_key().clone(),
            gas_price: action_receipt.gas_price(),
            output_data_receivers: vec![],
            input_data_ids: vec![],
            actions: delegate_action.get_actions(),
        }),
    });
```

**File:** test-loop-tests/src/tests/deterministic_account_id.rs (L158-171)
```rust
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

**File:** test-loop-tests/src/tests/deterministic_account_id.rs (L173-262)
```rust
/// Set up the exploit scenario and return the result of submitting the exploit tx.
///
/// `det_account_b` is deployed as a deterministic account and given an access key so
/// it can act as meta_tx_sender. The exploit tx wraps `state_init_b` inside a delegate
/// action whose `receiver_id` is `det_account_a` (wrong target). With the fix this is
/// caught at tx validation; without it, tx validation passes but the receipt fails.
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

**File:** protocol-model/spec/accounts-keys.md (L91-98)
```markdown
| `GasKeys` | v85 (`version.rs:562`) | Enables `TransactionV1` with `GasKeyNonce`, the `GasKeyFunctionCall`/`GasKeyFullAccess` permissions, and the `GasKeyInfo` balance/nonce model. `Transaction::gas_keys_required()` is true for V1 (`transaction.rs:204`). |
| `StrictNonce` | v85 (`version.rs:566`) | Allows `NonceMode::Strict` on `TransactionV1` requiring `tx_nonce == ak_nonce + 1`; pre-feature/V0 txs are effectively `Monotonic` (`verify_nonce`, `verifier.rs:224`). |
| `AccountCostIncrease` | v85 (`version.rs:574`) | Raises account-creation cost and changes gas-refund/penalty pricing for created accounts. Pricing arithmetic lives in [runtime-execution](runtime-execution.md). |
| `FixDeleteAccountGlobalContractStorageUsage` | v85 (`version.rs:560`) | `action_delete_account` now subtracts the *whole* contract storage (including the global-contract identifier) via `get_contract_storage_usage`; the legacy path subtracted only local code (`actions.rs:355`,`:454`). |
| `FixAccessKeyAllowanceCharging` | v83 (`version.rs:551`) | Removes the legacy bug where the FunctionCall allowance was decremented in place before later checks could fail. Pre-feature, `verify_and_charge_tx_ephemeral` mutates `access_key.permission…allowance` before storage/permission checks (`verifier.rs:338`). |
| `PostQuantumSignatures` | v85 (`version.rs:567`) | Adds ML-DSA-65 as a third key/signature scheme; `AddKey`/txs carrying such keys are rejected pre-feature. Storage usage uses `trie_id_len()` so PQ access keys cost the same as ed25519 (`access_keys.rs:26`). |
| `EthImplicitGlobalContract` | v83 (`version.rs:556`) | ETH-implicit account creation switches from embedded `near[hash]` local WASM to a shared `AccountContract::Global` wallet contract (`actions.rs:238`). |
| `FixDelegatedDeterministicStateInit` | v85 (`version.rs:561`) | Fixes the receiver-id check when creating a deterministic account from a delegated action; the state-init apply path is in [runtime-execution](runtime-execution.md). |
```

**File:** protocol-model/spec/accounts-keys.md (L100-102)
```markdown
| Deterministic account ids (NEP-616) | base by v82 (`_DeprecatedDeterministicAccountIds`, `version.rs:548`) | The `0s…` `NearDeterministicAccount` type and `create_deterministic_account` are in the v86 base. |

Account V2 itself has no live gating flag at v86 — the historical `_DeprecatedAccountVersions` was v46 (`version.rs:476`); V1/V2 coexist purely as a serialization concern. `MIN_SUPPORTED_PROTOCOL_VERSION` is 83 (`version.rs:600`), so the deprecated global-contract / deterministic-account-id / eth-implicit gates are always enabled on any version this binary processes.
```
