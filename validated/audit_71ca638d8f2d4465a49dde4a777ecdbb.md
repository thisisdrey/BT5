## Title
Postponed and Promise-Yield receipts are excluded from congestion-control byte accounting, letting an ordinary account grow unbounded, un-throttled trie state — (File: `runtime/runtime/src/congestion_control.rs`)

### Summary
NEP-539 congestion control is supposed to bound the total memory consumed by all receipts that a shard is forced to hold in its trie while it cannot immediately execute them (`max_congestion_memory_consumption`, described as "memory space of **all delayed and buffered receipts** in a shard"). In practice the byte-size counter (`CongestionInfoV1.receipt_bytes`) is only updated for the **delayed** queue and the **outgoing buffer**. Postponed action receipts (waiting on `input_data_ids`) and Promise-Yield receipts (waiting on an explicit resume/timeout) are written into state without ever calling `CongestionInfo::add_receipt_bytes`, even though the doc comment on the field explicitly claims it covers "delayed, buffered, postponed, or yielded" receipts.

### Finding Description
`compute_receipt_congestion_gas` treats `PromiseYield` (and postponed-eligible `Data`) receipts as contributing **zero** congestion gas/bytes "because the congestion control MVP does not account for data receipts or postponed receipts" [1](#0-0) .

Consistent with that, the actual write paths for these receipt kinds never touch `own_congestion_info`/`CongestionInfo`:
- A `PromiseYield` receipt received by the runtime is simply persisted with `set_promise_yield_receipt(state_update, receipt)` — no congestion accounting call at all [2](#0-1) .
- A postponed action receipt (missing `input_data_ids`) is persisted with `set_postponed_receipt(state_update, receipt)` — again no congestion accounting call [3](#0-2) .

By contrast, the two paths that *are* covered explicitly add/remove bytes: the delayed-queue wrapper (`DelayedReceiptQueueWrapper::push`/`pop`) [4](#0-3)  and the outgoing buffer (`ReceiptSink::buffer_receipt` calling `add_receipt_bytes`) [5](#0-4) .

Yet the struct doc explicitly states `receipt_bytes` is meant to be the "size of borsh serialized receipts stored in state because they were delayed, buffered, **postponed**, or **yielded**" [6](#0-5) , and the config doc for the throttle knob says it should cover "memory space of all delayed and buffered receipts in a shard" as the 100%-congested threshold [7](#0-6) . Neither promise is honored for postponed/promise-yield receipts.

This is structurally the same bug class as the Besu advisory: a buffer that is supposed to be bounded by total byte size is instead effectively unbounded (here, not even count-bounded) because the size accounting silently omits certain admitted item kinds — an ordinary, unprivileged account can populate that unaccounted store with attacker-sized payloads.

### Impact Explanation
Any ordinary account can, from an unprivileged deployed contract, create `PromiseYield` receipts (`promise_yield_create`) or ordinary cross-contract callback receipts that remain postponed pending a `Data` receipt. Each such receipt can carry large `FunctionCall` action arguments (bounded only by `max_receipt_size`) and is written into the trie under `TrieKey::PromiseYieldReceipt` / `TrieKey::PostponedReceipt`. None of this contributes to `CongestionInfo.receipt_bytes`, so the shard's own view of "memory congestion" (`max_congestion_memory_consumption`, used by `CongestionControl::congestion_level`) never reflects this growth [8](#0-7) . Because the memory-congestion signal is one of the few mechanisms in the protocol that throttles new incoming work when a shard's total receipt-holding state grows too large, an attacker can keep pumping receipts of this kind while the congestion machinery reports the shard as healthy, growing chunk-apply IO/memory (trie reads/writes, storage proof size) with no protocol-level backpressure targeting this specific growth vector — mirroring the Besu bug where oversized buffered items evaded a size-based safety limit that the design intended to enforce.

### Likelihood Explanation
Reachable from an ordinary account with no special privileges: creating cross-contract calls with unresolved data dependencies, or `promise_yield_create` receipts, is standard contract functionality, requires only prepaid gas for the created receipts, and does not require any validator or protocol-level privilege. The gap is a straightforward code/doc mismatch (accounting simply not implemented for these two receipt kinds) rather than a subtle race, making it easy to trigger deterministically.

### Recommendation
Add congestion-byte accounting (`CongestionInfo::add_receipt_bytes` / `remove_receipt_bytes`) at the points where postponed action receipts and Promise-Yield receipts are inserted into (`set_postponed_receipt`, `set_promise_yield_receipt`) and removed from state, mirroring what `DelayedReceiptQueueWrapper` and `ReceiptSink::buffer_receipt` already do for the delayed queue and outgoing buffer, so `max_congestion_memory_consumption` actually reflects the full set of receipts a shard is holding, as its own documentation claims.

### Proof of Concept
1. Deploy a contract on account `attacker.near`.
2. Repeatedly submit transactions that call a method which:
   - either issues `promise_yield_create` with a large attached payload and never issues the matching `promise_yield_resume`, or
   - issues a cross-contract `FunctionCall` receipt with a manufactured `input_data_ids` dependency on a data id that is never delivered before the promise-yield timeout window,
   each carrying arguments sized close to `max_receipt_size`.
3. Observe that each such receipt is persisted via `set_promise_yield_receipt`/`set_postponed_receipt` [2](#0-1) [3](#0-2)  without any corresponding call to `CongestionInfo::add_receipt_bytes`.
4. Confirm via the exposed `near_congestion_receipt_bytes` metric / `CongestionInfo.receipt_bytes()` that the shard's reported congestion "memory" level does not increase despite state trie growth from the accumulated large receipts, i.e., the byte-size safety limit is silently bypassed for this class of buffered state.

**Uncertainty note:** I was unable to fully verify the exact value/tunability of `yield_timeout_length_in_blocks` (whether it is a fixed protocol constant or influenced per-call) and could not confirm within the available index whether any separate storage-staking charge applies to postponed/promise-yield trie entries that would otherwise limit this growth economically; both would affect the ultimate severity/duration of the exposure and would need to be checked in a full repo checkout.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L465-500)
```rust
    /// Put a receipt in the outgoing receipt buffer of a shard.
    fn buffer_receipt(
        &mut self,
        receipt: Receipt,
        size: u64,
        gas: Gas,
        state_update: &mut TrieUpdate,
        shard: ShardId,
        use_state_stored_receipt: bool,
    ) -> Result<(), RuntimeError> {
        let receipt = match use_state_stored_receipt {
            true => {
                let metadata =
                    StateStoredReceiptMetadata { congestion_gas: gas, congestion_size: size };
                let receipt = StateStoredReceipt::new_owned(receipt, metadata);
                let receipt = ReceiptOrStateStoredReceipt::StateStoredReceipt(receipt);
                receipt
            }
            false => ReceiptOrStateStoredReceipt::Receipt(std::borrow::Cow::Owned(receipt)),
        };

        self.own_congestion_info.add_receipt_bytes(size)?;
        self.own_congestion_info.add_buffered_receipt_gas(gas)?;

        if receipt.should_update_outgoing_metadatas() {
            self.outgoing_metadatas.update_on_receipt_pushed(
                shard,
                ByteSize::b(size),
                gas,
                state_update,
            )?;
        }

        self.outgoing_buffers.to_shard(shard).push_back(state_update, &receipt)?;
        self.stats.buffered_receipts.entry(shard).or_default().add_receipt(size, gas);
        Ok(())
```

**File:** runtime/runtime/src/congestion_control.rs (L687-712)
```rust
        VersionedReceiptEnum::Data(_data_receipt) => {
            // Data receipts themselves don't cost gas to execute, their cost is
            // burnt at creation. What we should count, is the gas of the
            // postponed action receipt. But looking that up would require
            // reading the postponed receipt from the trie.
            // Thus, the congestion control MVP does not account for data
            // receipts or postponed receipts.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::PromiseYield(_) => {
            // The congestion control MVP does not account for yielding a
            // promise. Yielded promises are confined to a single account, hence
            // they never cross the shard boundaries. This makes it irrelevant
            // for the congestion MVP, which only counts gas in the outgoing
            // buffers and delayed receipts queue.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::PromiseResume(_) => {
            // The congestion control MVP does not account for resuming a promise.
            // Unlike `PromiseYield`, it is possible that a promise-resume ends
            // up in the delayed receipts queue.
            // But similar to a data receipt, it would be difficult to find the cost
            // of it without expensive state lookups.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::GlobalContractDistribution(_) => Ok(Gas::ZERO),
```

**File:** runtime/runtime/src/congestion_control.rs (L838-866)
```rust
    pub(crate) fn push(
        &mut self,
        trie_update: &mut TrieUpdate,
        receipt: &Receipt,
        apply_state: &ApplyState,
    ) -> Result<(), RuntimeError> {
        let config = &apply_state.config;

        let gas = compute_receipt_congestion_gas(&receipt, &config)?;
        let size = compute_receipt_size(&receipt)? as u64;

        // TODO It would be great to have this method take owned Receipt and
        // get rid of the Cow from the Receipt and StateStoredReceipt.
        let receipt = match config.use_state_stored_receipt {
            true => {
                let metadata =
                    StateStoredReceiptMetadata { congestion_gas: gas, congestion_size: size };
                let receipt = StateStoredReceipt::new_borrowed(receipt, metadata);
                ReceiptOrStateStoredReceipt::StateStoredReceipt(receipt)
            }
            false => ReceiptOrStateStoredReceipt::Receipt(Cow::Borrowed(receipt)),
        };

        self.new_delayed_gas = self.new_delayed_gas.checked_add(gas).ok_or(IntegerOverflowError)?;
        self.new_delayed_bytes =
            self.new_delayed_bytes.checked_add(size).ok_or(IntegerOverflowError)?;
        self.queue.push_back(trie_update, &receipt)?;
        Ok(())
    }
```

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```

**File:** runtime/runtime/src/lib.rs (L1642-1655)
```rust
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
        }
```

**File:** core/primitives/src/congestion_info.rs (L460-470)
```rust
pub struct CongestionInfoV1 {
    /// Sum of gas in currently delayed receipts.
    pub delayed_receipts_gas: u128,
    /// Sum of gas in currently buffered receipts.
    pub buffered_receipts_gas: u128,
    /// Size of borsh serialized receipts stored in state because they
    /// were delayed, buffered, postponed, or yielded.
    pub receipt_bytes: u64,
    /// If fully congested, only this shard can forward receipts.
    pub allowed_shard: u16,
}
```

**File:** core/parameters/src/config.rs (L150-158)
```rust
    /// How much memory space of all delayed and buffered receipts in a shard is
    /// considered 100% congested.
    ///
    /// Memory congestion contributes to overall congestion, which reduces how much
    /// other shards are allowed to forward to this shard.
    ///
    /// This threshold limits memory requirements of validators to a degree but it
    /// is not a hard guarantee.
    pub max_congestion_memory_consumption: u64,
```

**File:** protocol-model/spec/cross-shard-congestion.md (L188-197)
```markdown
### 5. Congestion control math (NEP-539)

`CongestionControl::congestion_level` (`congestion_info.rs:44`) is the **max** of four
fractions, each clamped to [0,1] (`clamped_f64_fraction`, `:474`):

- incoming = `delayed_receipts_gas / max_congestion_incoming_gas` (`:331`)
- outgoing = `buffered_receipts_gas / max_congestion_outgoing_gas` (`:338`)
- memory = `receipt_bytes / max_congestion_memory_consumption` (`:345`)
- missed-chunks = `missed_chunks_count / max_congestion_missed_chunks`, but 0 when
  `missed_chunks_count <= 1` (`:68`)
```
