### Title
Unvalidated attacker-controlled `length`/offset fields in `SharableTransactionRegion` lead to out-of-bounds slice construction in the Agave external-scheduler bridge - ([File: scheduling-utils/src/transaction_ptr.rs])

### Summary
The Firedancer report describes `fd_poh`'s `during_frag` trusting an mcache `sz` field from a producer tile and subtracting a fixed struct size from it without a lower-bound check, causing an integer underflow that is then fed into `memcpy`. Agave has a structurally analogous trust boundary in its external-scheduler bridge (`agave-scheduler-bindings` / `scheduling-utils`): a separate, sandboxed "pack"/scheduler process communicates with the validator over shared memory using `shaq` SPSC queues and a shared `rts_alloc::Allocator`. Messages such as `TpuToPackMessage`, `PackToWorkerMessage`, and `WorkerToPackMessage` carry raw `offset`/`length` fields (`SharableTransactionRegion`, `SharableTransactionBatchRegion`, `TransactionResponseRegion`) that are used directly to construct pointers and slices via `unsafe` code, without any bounds validation against the allocator's actual memory region.

### Finding Description
`TransactionPtr::from_sharable_transaction_region` takes an untrusted `SharableTransactionRegion.length: u32` (attacker/producer supplied, coming from the external pack process) and stores it as `count`, later used to build a raw slice: [1](#0-0) [2](#0-1) 

The safety contract merely states "`sharable_transaction_region` must reference a valid offset and length within the `allocator`" — this is a caller obligation, not something checked at runtime by `allocator.ptr_from_offset()`. This is used from `SchedulerBindingsBridge::drain_tpu`, which reads a `TpuToPackMessage` straight off the shared queue and immediately builds a `TransactionPtr` from it before any sanitization occurs: [3](#0-2) 

The same unchecked pattern repeats for batches (`TransactionPtrBatch::from_sharable_transaction_batch_region`, driven by `num_transactions` and `transactions_offset` from a `PackToWorkerMessage`/`WorkerToPackMessage`): [4](#0-3) [5](#0-4) 

and for `PubkeysPtr`/`CheckResponsesPtr`/`ExecutionResponsesPtr`, all of which build `core::slice::from_raw_parts` directly from an attacker-suppliable `count`/`offset` pair: [6](#0-5) 

I was unable to locate the `rts_alloc::Allocator` crate source in the index (it does not appear under any indexed path), so I could not confirm whether `Allocator::ptr_from_offset`/`allocate`/`offset` perform bounds checking against the shared-memory region size internally. This is a material gap: if the allocator itself clamps/validates `offset` against the mapped region size, out-of-bounds reads from a corrupt `offset` would be prevented (though an oversized `length`/`num_transactions` combined with a valid-but-near-the-end `offset` could still walk off the end of the mapping, since `count`/`length`/`num_transactions` are not checked against remaining allocation size in any of the functions shown above).

### Impact Explanation
This is the same class of bug as the Firedancer report: a size/offset value originating from a separate, less-trusted process (the external "pack"/scheduler, analogous to a Firedancer tile) is trusted to build a memory region (pointer + length) with no independent bounds validation, and that region is subsequently read (and in some paths freed) via `unsafe` raw-pointer operations. If the allocator does not itself enforce that `offset + length` (or `offset + num_transactions * struct_size`) stays within the shared mapping, a compromised or buggy external scheduler process can cause the Agave validator process to read (or free) out-of-bounds memory in the shared segment, potentially leading to a crash (denial of service) or, in the worst case, memory corruption/process compromise of the validator process — i.e., "Process to process Memory Corruption between sandboxed tiles," matching the report's impact category.

### Likelihood Explanation
This code path is only exercised when the validator is run with the external scheduler feature enabled (`spawn_external`, gated behind `#[cfg(unix)]` and requiring an external `agave-scheduler-bindings`-compatible process to attach). It is not on the default/always-on hot path for a stock validator, and the "external pack process" is a cooperating, deliberately-installed component rather than an arbitrary network peer — reducing but not eliminating exposure, since the threat model explicitly separates the pack process from the validator process as a sandboxing boundary (mirroring Firedancer's tile isolation), and a bug or compromise in that external process would directly translate to memory-safety violations in the validator.

### Recommendation
1. Verify (or add) explicit bounds checks in `rts_alloc::Allocator::ptr_from_offset`/`allocate`/`offset` that validate `offset` and any derived `offset + len` against the actual mapped shared-memory region size, returning an error/`None` rather than an unchecked pointer.
2. In `TransactionPtr::from_sharable_transaction_region`, `TransactionPtrBatch::from_sharable_transaction_batch_region`, `PubkeysPtr::from_sharable_pubkeys`, `CheckResponsesPtr`/`ExecutionResponsesPtr::from_transaction_response_region`, and `SchedulerBindingsBridge::handle_worker_response`, validate that `offset + length` (and `num_transactions * size_of::<T>()`) does not exceed the allocator's total mapped size before constructing any pointer/slice, instead of relying purely on doc-comment safety invariants.
3. Cap `length`/`num_transactions` fields against sane protocol maximums (e.g. `MAX_TRANSACTIONS_PER_MESSAGE`, max transaction size) at the point messages are dequeued from the `shaq` queues, before any `unsafe` pointer construction occurs.

### Proof of Concept
Not independently reproducible from the indexed codebase alone: exploitation would require attaching a malicious/compromised external scheduler process that sends a `TpuToPackMessage`/`PackToWorkerMessage`/`WorkerToPackMessage` with an `offset`/`length` (or `num_transactions`) pair that lies outside the bounds of the shared `rts_alloc` allocation, causing `TransactionPtr`/`TransactionPtrBatch`/`PubkeysPtr` construction in `scheduling-utils/src/transaction_ptr.rs` and `scheduling-utils/src/bridge/bindings.rs` to build an out-of-bounds slice/pointer that is then read or freed by the Agave validator process. Confirming exploitability requires inspecting `rts_alloc::Allocator`'s bounds-checking behavior, which was not present in the indexed codebase — a full Devin session with filesystem access would be needed to locate and audit that crate.

### Citations

**File:** scheduling-utils/src/transaction_ptr.rs (L17-21)
```rust
impl TransactionData for TransactionPtr {
    fn data(&self) -> &[u8] {
        unsafe { core::slice::from_raw_parts(self.ptr.as_ptr(), self.count) }
    }
}
```

**File:** scheduling-utils/src/transaction_ptr.rs (L45-58)
```rust
    /// # Safety
    /// - `sharable_transaction_region` must reference a valid offset and length
    ///   within the `allocator`.
    pub unsafe fn from_sharable_transaction_region(
        sharable_transaction_region: &SharableTransactionRegion,
        allocator: &Allocator,
    ) -> Self {
        // SAFETY: `sharable_transaction_region.offset` was allocated by `allocator`.
        let ptr = unsafe { allocator.ptr_from_offset(sharable_transaction_region.offset) };
        Self {
            ptr,
            count: sharable_transaction_region.length as usize,
        }
    }
```

**File:** scheduling-utils/src/transaction_ptr.rs (L115-143)
```rust
    /// # Safety
    /// - [`SharableTransactionBatchRegion`] must reference a valid offset and length
    ///   within the `allocator`.
    /// - ALL [`SharableTransactionRegion`]  within the batch must be valid.
    ///   See [`TransactionPtr::from_sharable_transaction_region`] for details.
    /// - `M` must match the actual `M` used within this allocation.
    pub unsafe fn from_sharable_transaction_batch_region(
        sharable_transaction_batch_region: &SharableTransactionBatchRegion,
        allocator: &'a Allocator,
    ) -> Self {
        // SAFETY: `sharable_transaction_batch_region.transactions_offset` was allocated by `allocator`.
        let base = unsafe {
            allocator.ptr_from_offset(sharable_transaction_batch_region.transactions_offset)
        };
        let tx_ptr = base.cast();
        // SAFETY:
        // - Assuming the batch was originally allocated to support `M`, this call will also be
        //   safe.
        let meta_ptr = unsafe { base.byte_add(Self::TRANSACTION_META_START).cast() };

        Self {
            tx_ptr,
            meta_ptr,
            num_transactions: usize::from(sharable_transaction_batch_region.num_transactions),
            allocator,

            _meta: PhantomData,
        }
    }
```

**File:** scheduling-utils/src/bridge/bindings.rs (L237-256)
```rust
    pub fn drain_tpu(
        &mut self,
        mut callback: impl FnMut(&mut Self, TransactionKey) -> TxDecision,
        max_count: usize,
    ) -> usize {
        self.tpu_to_pack.sync();

        let additional = std::cmp::min(self.tpu_to_pack.len(), max_count);
        let mut sanitize_failures = 0usize;
        for _ in 0..additional {
            let msg = self.tpu_to_pack.try_read().expect("len checked above");

            // SAFETY:
            // - Trust Agave to have properly transferred ownership to use & not to
            //   free/access this.
            // - We are only creating a single exclusive pointer.
            let tx = unsafe {
                TransactionPtr::from_sharable_transaction_region(&msg.transaction, &self.allocator)
            };

```

**File:** core/src/banking_stage/consume_worker.rs (L410-428)
```rust
                )
            };
            let (translation_results, transactions, max_ages) =
                Self::translate_transaction_batch(&batch, bank);

            // Enforce all or nothing on translation_results.
            let execution_flags = ExecutionFlags {
                drop_on_failure: message.flags & execution_flags::DROP_ON_FAILURE != 0,
                all_or_nothing: message.flags & execution_flags::ALL_OR_NOTHING != 0,
            };
            if execution_flags.all_or_nothing && translation_results.len() != transactions.len() {
                self.send_execution_response(
                    message,
                    Self::all_or_nothing_translate_iterator(&translation_results, bank.slot()),
                )?;

                return Ok(false);
            }
            let output = self.consumer.process_and_record_aged_transactions(
```

**File:** scheduling-utils/src/pubkeys_ptr.rs (L36-55)
```rust
    pub unsafe fn from_sharable_pubkeys(
        sharable_pubkeys: &SharablePubkeys,
        allocator: &Allocator,
    ) -> Self {
        assert_ne!(sharable_pubkeys.num_pubkeys, 0);
        // SAFETY: `sharable_pubkeys.offset` was allocated by `allocator`.
        let ptr = unsafe { allocator.ptr_from_offset(sharable_pubkeys.offset) }.cast();

        Self {
            ptr,
            count: sharable_pubkeys.num_pubkeys as usize,
        }
    }

    /// Returns the allocation as a slice.
    pub fn as_slice(&self) -> &[Pubkey] {
        // SAFETY
        // - Constructor invariants guarantee that we don't overrun the end of the allocation.
        unsafe { core::slice::from_raw_parts(self.ptr.as_ptr(), self.count) }
    }
```
