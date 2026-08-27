#No Vulnerability found for this question.

The scenario described is not a vulnerability but rather the intended behavior of NEAR's congestion-control system. The `outgoing_receipts_usual_size_limit`/`outgoing_receipts_big_size_limit` mechanism is a protocol-level fairness/backpressure control applied uniformly to all outgoing receipts from a shard to a shard-pair, regardless of which contract or account produced them [1](#0-0) . It buffers any receipts exceeding the per-block size limit rather than dropping them or corrupting execution, and forwards buffered receipts in later blocks [2](#0-1) .

The `WalletContract::rlp_execute` → `nep_141_storage_balance_callback` fan-out does produce a legitimate 2-action receipt chain (`storage_deposit` + `ft_transfer`) when the NEP-141 receiver is unregistered [3](#0-2) , and each individual wallet contract only has one transaction in flight at a time due to the `has_in_flight_tx` invariant [4](#0-3) . However:

1. Producing many maximal-size, valid receipts is exactly what a busy shard is expected to handle, and the congestion-control size limits (100 KiB usual / 4.5 MiB big) exist specifically to bound and fairly share this bandwidth across all senders on a shard-pair basis, not per-account [5](#0-4) . This is a designed throughput/fairness mechanism, not something an attacker can "game" beyond what it's built to tolerate — any single source (Sybil or not) saturating a shard-pair's link merely causes its own excess receipts to buffer and be delayed, which is the intended congestion response [6](#0-5) .
2. There is no mechanism by which sending many legitimate-sized receipts causes state-root divergence, fund loss/freezing, double-spend, or a shard halt — worst case is temporary throughput degradation, which the rules explicitly instruct to reject as "speculative resource-hygiene claims with no reachable mainnet scenario" absent concrete fund/consensus impact.
3. The `allowed_shard` round-robin and bandwidth scheduler further limit how much sustained advantage any one shard (and therefore any set of Sybil accounts concentrated there) can gain over time [7](#0-6) .

Since the described behavior is bounded, self-correcting (buffering, not failure), and does not lead to any of the required concrete impacts (fund loss/freezing, double-spend, authorization escalation, consensus divergence, or shard halt), it does not meet the bar for a valid finding under the stated rules.

### Citations

**File:** core/primitives/src/congestion_info.rs (L102-111)
```rust
    /// How much data another shard can send to us in the next block.
    pub fn outgoing_size_limit(&self, sender_shard: ShardId) -> u64 {
        if sender_shard == ShardId::from(self.info.allowed_shard()) {
            // The allowed shard is allowed to send more data to us.
            self.config.outgoing_receipts_big_size_limit
        } else {
            // Other shards have a low standard limit.
            self.config.outgoing_receipts_usual_size_limit
        }
    }
```

**File:** core/primitives/src/congestion_info.rs (L360-384)
```rust
    pub fn finalize_allowed_shard(
        &mut self,
        own_shard: ShardId,
        all_shards: &[ShardId],
        congestion_seed: u64,
    ) {
        let allowed_shard = Self::get_new_allowed_shard(own_shard, all_shards, congestion_seed);
        self.set_allowed_shard(allowed_shard.into());
    }

    fn get_new_allowed_shard(
        own_shard: ShardId,
        all_shards: &[ShardId],
        congestion_seed: u64,
    ) -> ShardId {
        if let Some(index) = congestion_seed.checked_rem(all_shards.len() as u64) {
            // round robin for other shards based on the seed
            return *all_shards
                .get(index as usize)
                .expect("`checked_rem` should have ensured array access is in bound");
        }
        // checked_rem failed, hence all_shards.len() is 0
        // own_shard is the only choice.
        return own_shard;
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L289-325)
```rust
    /// Put a receipt in the outgoing receipts vector (=forward) if the
    /// congestion preventing limits allow it. Put it in the buffered receipts
    /// queue otherwise.
    pub(crate) fn forward_or_buffer_receipt(
        &mut self,
        receipt: Receipt,
        apply_state: &ApplyState,
        state_update: &mut TrieUpdate,
    ) -> Result<(), RuntimeError> {
        let shard = receipt.receiver_shard_id(&self.info.shard_layout)?;
        let size = compute_receipt_size(&receipt)?;
        let gas = compute_receipt_congestion_gas(&receipt, &apply_state.config)?;

        match ReceiptSinkV2::try_forward(
            receipt,
            gas,
            size,
            shard,
            &mut self.sink.outgoing_limit,
            &mut self.sink.outgoing_receipts,
            apply_state,
            &mut self.sink.stats,
        )? {
            ReceiptForwarding::Forwarded => (),
            ReceiptForwarding::NotForwarded(receipt) => {
                self.sink.buffer_receipt(
                    receipt,
                    size,
                    gas,
                    state_update,
                    shard,
                    apply_state.config.use_state_stored_receipt,
                )?;
            }
        }
        Ok(())
    }
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

**File:** runtime/near-wallet-contract/implementation/wallet-contract/src/lib.rs (L239-269)
```rust
            None => {
                // receiver_id is not registered so we must call `storage_deposit` first.
                let storage_deposit_args =
                    format!(r#"{{"account_id": "{receiver_id}"}}"#).into_bytes();
                let transfer_function_call = match action {
                    near_action::Action::FunctionCall(x) => x,
                    _ => {
                        return PromiseOrValue::Value(ExecuteResponse {
                            success: false,
                            success_value: None,
                            error: Some(
                                "Expected function call action to perform NEP-141 transfer".into(),
                            ),
                        });
                    }
                };
                Promise::new(token_id)
                    .function_call(
                        "storage_deposit".into(),
                        storage_deposit_args,
                        NEP_141_STORAGE_DEPOSIT_AMOUNT,
                        NEP_141_STORAGE_DEPOSIT_GAS,
                    )
                    .function_call(
                        transfer_function_call.method_name,
                        transfer_function_call.args,
                        transfer_function_call.deposit,
                        transfer_function_call.gas,
                    )
                    .then(ext.rlp_execute_callback(caller_deposit))
            }
```

**File:** core/parameters/src/config.rs (L219-227)
```rust
    /// The standard size limit for outgoing receipts aimed at a single shard.
    /// This limit is pretty small to keep the size of source_receipt_proofs under control.
    /// It limits the total sum of outgoing receipts, not individual receipts.
    pub outgoing_receipts_usual_size_limit: u64,

    /// Large size limit for outgoing receipts to a shard, used when it's safe
    /// to send a lot of receipts without making the state witness too large.
    /// It limits the total sum of outgoing receipts, not individual receipts.
    pub outgoing_receipts_big_size_limit: u64,
```
