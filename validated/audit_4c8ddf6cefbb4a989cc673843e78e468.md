### Title
Gas-key exec fee overcharge silently burns user funds outside the protocol's burn/refund accounting - ([File: runtime/near-vm-runner/src/logic/logic.rs])

### Summary
The bug class in the report is a fee function that fails to apply the correct scaling/length to a value, resulting in a fee that is charged inconsistently with the "correct" scaled calculation used elsewhere in the same contract. The analogous nearcore issue is in the gas-key `AddKey` action's exec-fee pricing: the host-function path prices the fee on the raw wire length of an ML-DSA-65 public key instead of the on-trie identifier length used everywhere else, and this mismatch is only corrected behind a nightly-only protocol feature that is not active on the stable protocol version.

### Finding Description
`PublicKey` has two different notions of length for ML-DSA-65 keys: `len()` (borsh wire length, 1953 bytes) and `trie_id_len()` (on-trie identifier length, 33 bytes, since ML-DSA-65 keys are stored in the trie by hash) [1](#0-0) .

For a directly-signed transaction, `AddKey`'s exec fee is computed via `permission_exec_fees`, which unconditionally uses `public_key.trie_id_len()` for the gas-key nonce fee [2](#0-1) . This is correct and matches actual on-trie storage.

However, the same `AddKey`-with-gas-key-permission operation can also be triggered from inside a contract via the host functions `promise_batch_action_add_gas_key_with_full_access` / `promise_batch_action_add_gas_key_with_function_call`. These compute the exec fee using `gas_key_exec_pk_len`, which only uses `trie_id_len()` if the `fix_ml_dsa_cost_charging` config flag is set; otherwise it falls back to `pk_len`, the wire/borsh length (1953 bytes for ML-DSA-65) [3](#0-2) . This `pk_len` is then fed into `gas_key_add_key_exec_fee`, multiplying the per-nonce byte cost by the (wrongly large) key length [4](#0-3)  and reserved via `pay_gas_key_add_key_fees` [5](#0-4) .

The gating flag `fix_ml_dsa_cost_charging` is tied to `ProtocolFeature::FixMlDsaCostCharging`, which activates only at protocol version 153 - a nightly-only value far above `STABLE_PROTOCOL_VERSION = 87` [6](#0-5) [7](#0-6) . Meanwhile `PostQuantumSignatures` itself (which enables ML-DSA-65 keys/transactions) activates at protocol version 85, which is already below the stable version 87 [8](#0-7) . This means ML-DSA-65 gas keys are usable in the currently stable protocol configuration, but the corresponding cost-charging fix is not active.

The codebase's own regression test documents the consequence precisely: "the host path reserved the exec fee on `len()` (1953); the extra reserved gas was neither burnt nor refunded, so total supply silently dropped" [9](#0-8) . The test is gated to only run when `FixMlDsaCostCharging` is enabled, so under the stable build it is skipped entirely and does not protect production behavior [10](#0-9) .

### Impact Explanation
When a contract adds an ML-DSA-65 gas key to an account via a cross-contract promise batch action (a fully unprivileged, contract-reachable operation any deployed contract or user transaction can trigger), the runtime reserves/burns gas computed from the wire-format key length (1953 bytes) rather than the true on-trie storage length (33 bytes) that is actually used for state accounting elsewhere. Per the codebase's own analysis, this excess reserved gas is neither burnt into the standard burn accounting nor refunded to the caller, meaning tokens are permanently destroyed from an account's balance without being accounted for as burnt or subsidized amounts — a silent, unrecoverable loss/deflation of user funds (token loss), matching the accepted impact category.

### Likelihood Explanation
The action is reachable by any account/contract that can (a) construct or receive an ML-DSA-65 access key (a stable, non-privileged feature since protocol version 85) and (b) issue a `promise_batch_action_add_gas_key_with_full_access`/`..._with_function_call` host call from a deployed contract — an ordinary, permissionless, self-service operation. No validator, network, or operator privilege is required. The bug is deterministic given the fee-computation code path shown, and is explicitly reproduced by the codebase's own (currently-skipped-on-stable) regression test.

### Recommendation
Decouple the exec-fee pricing for the gas-key `AddKey` host-function path from the `fix_ml_dsa_cost_charging` nightly gate, i.e., make `gas_key_exec_pk_len` always use `PublicKey::trie_id_len()` (matching the already-unconditional behavior in `runtime/runtime/src/config.rs::permission_exec_fees`), or otherwise activate `ProtocolFeature::FixMlDsaCostCharging` at or before the protocol version where `PostQuantumSignatures` is enabled, so the fix ships together with the feature it corrects rather than lagging behind it on stable.

### Proof of Concept
The codebase's own test demonstrates the exact fault condition (only executed under the nightly feature gate, and skipped/inactive on the stable protocol version): [11](#0-10) 
This test deploys a contract that self-adds an ML-DSA-65 gas key via `batch_create` + `action_add_gas_key_with_full_access`, drains the resulting receipt cascade to settlement, and asserts that the account's balance loss equals recorded burnt/destroyed amounts. Under the unfixed (stable) configuration, the exec fee reserved for this action is computed with `pk_len` (1953) instead of `trie_id_len()` (33) per `gas_key_exec_pk_len` [3](#0-2) , producing exactly the supply leak the test's own doc-comment describes.

### Citations

**File:** docs/architecture/how/post_quantum_signatures.md (L129-141)
```markdown
### 5. Storage usage and fee plumbing

The storage-stake calculation
(`runtime/runtime/src/access_keys.rs::access_key_storage_usage`) and the
gas-key fee helpers (`gas_key_*_fee` in `runtime/runtime/src/config.rs`) use
`PublicKey::trie_id_len()` rather than `PublicKey::len()`:

- `len()` reports the borsh-encoded length (33 / 65 / 1953 across the
  three `PublicKey` variants).
- `trie_id_len()` reports the on-trie length (33 / 65 / **33**).

The two diverge only for `PublicKey::MLDSA65`. Every storage-stake and
trie-byte-priced fee path was updated to call `trie_id_len()`.
```

**File:** runtime/runtime/src/config.rs (L400-412)
```rust
    // Additional costs for adding an access key with GasKeyFunctionCall or GasKeyFullAccess permissions.
    let gas_key_info = match permission {
        AccessKeyPermission::GasKeyFullAccess(info)
        | AccessKeyPermission::GasKeyFunctionCall(info, _) => info,
        _ => return key_fee,
    };
    let nonce_fee = gas_key_add_key_exec_fee(
        fees,
        account_id.len(),
        public_key.trie_id_len(),
        gas_key_info.num_nonces,
    );
    key_fee.checked_add(nonce_fee.total()).unwrap()
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L262-279)
```rust
/// Public-key byte length for a gas-key EXEC (storage) fee computation. Once the
/// fix is enabled this is the on-trie identifier length; otherwise it falls back
/// to `pk_len` (the decoded key's wire length, same as the send fee), preserving
/// the pre-fix behavior.
pub(crate) fn gas_key_exec_pk_len(
    public_key_res: &Result<near_crypto::PublicKey>,
    config: &Config,
    pk_len: usize,
) -> usize {
    match public_key_res {
        // Exec (storage) fee should reflect how many bytes the key occupies in
        // storage, not on the wire.
        Ok(pk) if config.fix_ml_dsa_cost_charging => pk.trie_id_len(),
        // Preserve the existing behavior if the fix is not enabled (or the key
        // failed to decode); changing it would break protocol consensus.
        _ => pk_len,
    }
}
```

**File:** core/parameters/src/cost.rs (L886-905)
```rust
pub fn gas_key_add_key_exec_fee(
    cfg: &RuntimeFeesConfig,
    account_id_len: usize,
    public_key_len: usize,
    num_nonces: NonceIndex,
) -> GasKeyAddFee {
    let num_nonces = num_nonces as u64;
    let base =
        cfg.fee(ActionCosts::gas_key_nonce_write_base).exec_fee().checked_mul(num_nonces).unwrap();
    let nonce_key_len =
        access_key_key_len(account_id_len, public_key_len) + std::mem::size_of::<NonceIndex>();
    let per_byte = cfg
        .fee(ActionCosts::gas_key_byte)
        .exec_fee()
        .checked_mul((nonce_key_len + AccessKey::NONCE_VALUE_LEN) as u64)
        .unwrap()
        .checked_mul(num_nonces)
        .unwrap();
    GasKeyAddFee { base, per_byte }
}
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L3491-3506)
```rust
    pay_action_base(
        &mut ctx.result_state.gas_counter,
        &ctx.fees_config,
        ActionCosts::add_full_access_key,
        sir,
    )?;
    let receiver_id = ctx.ext.get_receipt_receiver(receipt_idx);
    let send_fee = gas_key_add_key_send_fee(&ctx.fees_config, sir);
    let exec_fee = gas_key_add_key_exec_fee(
        &ctx.fees_config,
        receiver_id.len(),
        gas_key_exec_pk_len(&public_key_res, &ctx.config, pk_len),
        num_nonces,
    );
    ctx.result_state.gas_counter.pay_gas_key_add_key_fees(send_fee, &exec_fee)?;
    ctx.ext.append_action_add_gas_key_with_full_access(receipt_idx, public_key_res?, num_nonces);
```

**File:** core/primitives-core/src/version.rs (L581-597)
```rust
            ProtocolFeature::FixDelegateActionDepositWithFunctionCallError
            | ProtocolFeature::FixDeleteAccountGlobalContractStorageUsage
            | ProtocolFeature::FixDelegatedDeterministicStateInit
            | ProtocolFeature::GasKeys
            | ProtocolFeature::ContinuousEpochSync
            | ProtocolFeature::DynamicResharding
            | ProtocolFeature::StickyReshardingValidatorAssignment
            | ProtocolFeature::StrictNonce
            | ProtocolFeature::PostQuantumSignatures
            | ProtocolFeature::UniqueChunkTransactions
            | ProtocolFeature::ValidateBlockOrdinalAndEpochSyncDataHash
            | ProtocolFeature::YieldWithId
            | ProtocolFeature::ExecutionMetadataV4
            | ProtocolFeature::SignedContractCodeResponse
            | ProtocolFeature::ClampOutgoingGasAdmission
            | ProtocolFeature::AccountCostIncrease
            | ProtocolFeature::DelegateV2 => 85,
```

**File:** core/primitives-core/src/version.rs (L605-612)
```rust

            // Nightly features:
            ProtocolFeature::FixContractLoadingCost => 129,
            // TODO(#11201): When stabilizing this feature in mainnet, also remove the temporary code
            // that always enables this for mocknet (see config_mocknet function).
            ProtocolFeature::ShuffleShardAssignments => 143,
            ProtocolFeature::EarlyKickout => 152,
            ProtocolFeature::FixMlDsaCostCharging => 153,
```

**File:** core/primitives-core/src/version.rs (L656-660)
```rust
/// Current protocol version used on the mainnet with all stable features.
const STABLE_PROTOCOL_VERSION: ProtocolVersion = 87;

// On nightly, pick big enough version to support all features.
const NIGHTLY_PROTOCOL_VERSION: ProtocolVersion = 157;
```

**File:** runtime/runtime/src/tests/apply.rs (L5334-5340)
```rust
/// A contract that creates an ML-DSA-65 gas key must not leak total supply.
///
/// Before this fix, the pre-execution / refund path priced the key on
/// `trie_id_len()` (33 bytes) while the host path reserved the exec fee on
/// `len()` (1953); the extra reserved gas was neither burnt nor refunded, so
/// total supply silently dropped. This guards against that regression: supply
/// is conserved now that the host exec fee also uses `trie_id_len()`.
```

**File:** runtime/runtime/src/tests/apply.rs (L5341-5450)
```rust
#[test]
fn test_gas_key_add_key_conserves_supply() {
    if !ProtocolFeature::FixMlDsaCostCharging.enabled(PROTOCOL_VERSION) {
        tracing::info!("skipping: FixMlDsaCostCharging not enabled at PROTOCOL_VERSION");
        return;
    }
    let initial_balance = Balance::from_near(1_000_000);
    let (runtime, tries, mut root, mut apply_state, signers, epoch_info_provider) = setup_runtime(
        vec![alice_account()],
        initial_balance,
        Balance::from_near(500_000),
        Gas::from_teragas(1000),
    );
    let shard_uid = ShardUId::single_shard();
    let gas_key: PublicKey = SecretKey::from_seed(KeyType::MLDSA65, "gas-key-seed").public_key();

    let alice_amount = |root: CryptoHash| {
        get_account(&tries.new_trie_update(shard_uid, root), &alice_account())
            .unwrap()
            .unwrap()
            .amount()
    };
    let before = alice_amount(root);

    // Single signed tx (deploy + call), so alice is fully debited up front and the
    // scenario is closed: total supply == alice's amount plus everything burnt.
    // The contract adds an ML-DSA-65 gas key to alice via a self-promise batch.
    use near_primitives::serialize::to_base64;
    let call_promise_args = serde_json::json!([
        {"batch_create": {"account_id": alice_account()}, "id": 0},
        {"action_add_gas_key_with_full_access": {
            "promise_index": 0,
            "public_key": to_base64(&borsh::to_vec(&gas_key).unwrap()),
            "num_nonces": 3,
        }, "id": 0},
    ]);
    let tx = SignedTransaction::from_actions(
        1,
        alice_account(),
        alice_account(),
        &*signers[0],
        vec![
            Action::DeployContract(DeployContractAction {
                code: near_test_contracts::rs_contract().to_vec(),
            }),
            Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "call_promise".to_string(),
                args: serde_json::to_vec(&call_promise_args).unwrap(),
                gas: MAX_ATTACHED_GAS,
                deposit: Balance::ZERO,
            })),
        ],
        CryptoHash::default(),
    );

    let mut incoming: Vec<Receipt> = vec![];
    let mut destroyed = Balance::ZERO;
    let mut settled = false;
    for round in 0..12 {
        let apply_result = runtime
            .apply(
                tries.get_trie_for_shard(shard_uid, root),
                &None,
                &apply_state,
                &incoming,
                if round == 0 {
                    SignedValidPeriodTransactions::new(vec![tx.clone()], vec![true])
                } else {
                    SignedValidPeriodTransactions::empty()
                },
                &epoch_info_provider,
                Default::default(),
            )
            .unwrap();
        // Value that left circulation this round. Receiver/validator rewards stay
        // in accounts (not counted here); subsidies and gas deficit are minted.
        let b = &apply_result.stats.balance;
        destroyed = destroyed
            .checked_add(b.tx_burnt_amount)
            .unwrap()
            .checked_add(b.slashed_burnt_amount)
            .unwrap()
            .checked_add(b.other_burnt_amount)
            .unwrap()
            .checked_sub(b.subsidized_amount)
            .unwrap()
            .checked_sub(b.gas_deficit_amount)
            .unwrap();
        root = commit_apply_result(&apply_result, &mut apply_state, &tries, shard_uid);
        incoming = apply_result.outgoing_receipts.clone();
        apply_state.block_height += 1;
        if round > 0 && incoming.is_empty() && apply_result.delayed_receipts_count == 0 {
            settled = true;
            break;
        }
    }
    // The supply accounting below is only meaningful once the whole receipt
    // cascade has drained; a run that hit the round cap would measure a partial
    // state and could mask (or fake) a leak.
    assert!(settled, "receipt cascade did not settle within the round budget");

    let supply_drop = before.checked_sub(alice_amount(root)).unwrap();
    assert_eq!(
        supply_drop.as_yoctonear(),
        destroyed.as_yoctonear(),
        "supply leak: alice lost {} yocto but only {} was recorded as destroyed",
        supply_drop.as_yoctonear(),
        destroyed.as_yoctonear(),
    );
}
```
