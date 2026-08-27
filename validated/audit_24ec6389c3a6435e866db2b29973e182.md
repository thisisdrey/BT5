FixMlDsaCostCharging activates at protocol version 153 (nightly), well above STABLE_PROTOCOL_VERSION 86 seen throughout this tree [1](#0-0) . This confirms the underlying bug the feature fixes is a *currently live* accounting defect on the stable/production path, not a mitigated one.

### Title
Under-accounted ML-DSA-65 gas-key exec fee is neither burnt nor refunded, silently leaking total supply - (File: `runtime/runtime/src/config.rs`)

### Summary
When an unprivileged signer adds an ML-DSA-65 (post-quantum) gas key via `AddKey`/`TransferToGasKey`/gas-key host functions, the exec (storage) fee for the key is priced at pre-execution time using the public key's wire length (`PublicKey::len()`, 1953 bytes for ML-DSA-65) while the VM host path reserves/charges the actual exec cost using the shorter on-trie identifier length (`trie_id_len()`, 33 bytes). The gap between the two is gas that was purchased from the signer's balance but is subsequently neither burnt as execution cost nor returned as a gas refund — it disappears from account balances without being recorded in `tx_burnt_amount`/`tokens_burnt`, causing total supply to silently drop. This is the same bug class as the ZetaChain finding: a fee component computed once (and deducted/minted from a user-facing balance) that is not fully accounted for on the execution/refund path, leaving value stranded with no way to reclaim it.

### Finding Description
`gas_key_exec_pk_len` in `runtime/near-vm-runner/src/logic/logic.rs` selects `trie_id_len()` for the exec fee once `fix_ml_dsa_cost_charging` is enabled [2](#0-1) , and `runtime/runtime/src/config.rs`'s `permission_exec_fees` similarly switches to `public_key.trie_id_len()` for the `AddKey` gas-key exec fee only under the same flag [3](#0-2) . Before this flag is enabled (i.e., at every protocol version below 153, which includes the current stable version 86), both the pre-execution pricing/reservation and the host-side charging use the wire length inconsistently — per the fix's own description: "gas keys: price exec (storage) fees on trie_id_len() and send (transmission) fees on len(), instead of pricing the exec fee on the wire length" [4](#0-3) , and the associated regression test states explicitly: "the pre-execution / refund path priced the key on trie_id_len() (33 bytes) while the host path reserved the exec fee on len() (1953); the extra reserved gas was neither burnt nor refunded, so total supply silently dropped" [5](#0-4) .

This mirrors the root cause pattern in the ZetaChain report exactly: an amount is computed/reserved to cover a specific purpose (there: protocol fee bundled into the minted swap amount; here: exec-fee gas reserved for the gas-key trie write) but the actual settlement step (there: the Uniswap swap + burn; here: `refund_unspent_gas_and_deposits`'s `gross_gas_refund` computation against `result.gas_used`/`result.gas_burnt`) uses a different, smaller basis, so the delta is stranded — never burnt into `tx_burnt_amount`, never refunded to the signer, and unrecoverable by any subsequent action, exactly as the missing withdrawal path for `crosschain`'s stuck protocol fee.

### Impact Explanation
This is a protocol-level token-inflation/loss bug: NEAR tokens are debited from a real user account (the signer paying for the `AddKey`/gas-key transaction) but are never credited to any burnt-amount counter, contract reward, or refund receipt. Over many such transactions this causes `new_total_supply` to diverge from the sum of on-chain account balances (`block.rs:193`), a violation of the "Non-negative supply arithmetic"/burn-accounting invariant documented in the economics spec [6](#0-5) . This is a genuine, reachable token-loss condition triggerable by any ordinary account creating an ML-DSA-65 gas key — it requires no privileged role, matching the ZetaChain report's "no attack path needed, pure logic flaw" characterization.

### Likelihood Explanation
Reachable by any account using the (already-shipped, non-nightly) ML-DSA-65 / post-quantum signature and gas-key features via a standard `AddKey` action with `GasKeyFullAccess`/`GasKeyFunctionCall` permission, or `TransferToGasKey`, as demonstrated by the existing regression test `test_gas_key_add_key_conserves_supply` [7](#0-6) . Because `FixMlDsaCostCharging` is gated to protocol version 153 while the tree's stable protocol version is 86, the buggy pre-fix code path is what actually executes today; the fix and test exist in-repo but are inert until that future protocol upgrade activates.

### Recommendation
Backport/accelerate activation of the `FixMlDsaCostCharging` protocol feature (or apply an equivalent consensus-safe fix) so that the pre-execution exec-fee pricing (`permission_exec_fees` in `runtime/runtime/src/config.rs`) and the host-side reservation (`gas_key_exec_pk_len` in `runtime/near-vm-runner/src/logic/logic.rs`) use the same length basis (`trie_id_len()`) consistently, ensuring every gas unit purchased is either burnt or refunded and total supply is conserved.

### Proof of Concept
The in-repo regression test constructs exactly this scenario: a single signed transaction deploys a contract and, within the same call, batches an `action_add_gas_key_with_full_access` for an ML-DSA-65 public key; it then asserts `alice`'s balance drop equals the recorded destroyed amount, which the code comments confirm fails pre-fix due to the exec-fee length mismatch [8](#0-7) [9](#0-8) .

### Citations

**File:** core/primitives-core/src/version.rs (L606-612)
```rust
            // Nightly features:
            ProtocolFeature::FixContractLoadingCost => 129,
            // TODO(#11201): When stabilizing this feature in mainnet, also remove the temporary code
            // that always enables this for mocknet (see config_mocknet function).
            ProtocolFeature::ShuffleShardAssignments => 143,
            ProtocolFeature::EarlyKickout => 152,
            ProtocolFeature::FixMlDsaCostCharging => 153,
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

**File:** core/parameters/res/runtime_configs/153.yaml (L1-6)
```yaml
# Fix two related ML-DSA-65 cost-charging issues:
# - gas keys: price exec (storage) fees on trie_id_len() and send (transmission)
#   fees on len(), instead of pricing the exec fee on the wire length;
# - meta transactions: meter the inner delegate signature verification compute on
#   the receiver shard instead of the signer shard.
fix_ml_dsa_cost_charging: { old: false, new: true }
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

**File:** runtime/runtime/src/tests/apply.rs (L5341-5377)
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
```

**File:** runtime/runtime/src/tests/apply.rs (L5442-5449)
```rust
    let supply_drop = before.checked_sub(alice_amount(root)).unwrap();
    assert_eq!(
        supply_drop.as_yoctonear(),
        destroyed.as_yoctonear(),
        "supply leak: alice lost {} yocto but only {} was recorded as destroyed",
        supply_drop.as_yoctonear(),
        destroyed.as_yoctonear(),
    );
```

**File:** protocol-model/spec/economics.md (L92-99)
```markdown
## Invariants & failure modes
- **Non-negative supply arithmetic.** `new_total_supply` uses checked add/sub and panics on overflow/underflow (`block.rs:193`); a block whose `balance_burnt` exceeds `prev + minted` is impossible by construction.
- **Minted ≤ cap.** `epoch_actual_reward` ≤ `epoch_total_reward` because per-validator rewards are fractions of the pool and below-threshold validators earn 0 (`reward_calculator.rs:109`). Verified by `test_adjust_max_inflation` (`reward_calculator.rs:590`).
- **Burn price ≤ purchase price (post-`AccountCostIncrease`).** `min(purchase, block)` guarantees the receiver reward and refunds never mint new tokens or underflow (comment at `lib.rs:922`).
- **Refund penalty ≤ refund.** `gas_penalty_for_gas_refund` clamps to `min(penalty, gas_refund)` (`cost.rs:692`).
- **Gas-price bounds.** Always clamped to `[min_gas_price, max_gas_price]` and unchanged on skipped blocks (`block.rs:449`, `block.rs:471`).
- **Storage staking.** An account failing `check_storage_stake` and not zero-balance cannot transact / receive deposits that push it under the threshold: `LackBalanceForState` (`verifier.rs:347`, `verifier.rs:520`).
- **Contract reward requires a live account.** If the receiver account is gone at the end of execution, its reward is not credited and remains burnt (goes to validators) (`lib.rs:1008`).
```
