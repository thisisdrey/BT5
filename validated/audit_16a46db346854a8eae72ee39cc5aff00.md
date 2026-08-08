## Finding

### Title
Address-lookup-table resolution is performed before blockhash validity check, allowing free CPU/accounts-db amplification via doomed-to-fail transactions - (File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs)

### Summary
`TransactionViewReceiveAndBuffer::try_handle_packet` (via `translate_to_runtime_view`) fully sanitizes a transaction and resolves every referenced address-lookup-table account before the caller ever checks whether the transaction's `recent_blockhash` is valid. Since blockhash age can be determined directly from the unresolved message (no ALT data needed), an attacker can send well-formed, validly-signed packets with a stale/garbage blockhash and many distinct ALT references to force costly ALT resolution work that is thrown away for zero fee.

### Finding Description
In `handle_packet_batch_message`, each packet is first passed to `Self::try_handle_packet`, which calls `translate_to_runtime_view`. That function performs, in order: `SanitizedTransactionView::try_new_sanitized`, a static lock-count check against `transaction_account_lock_limit`, and then `load_addresses_for_view`, which for every `V0` message walks all address-table-lookup entries and calls `bank.load_addresses_from_ref` [1](#0-0) . `load_addresses_from_ref` iterates every referenced lookup-table entry and calls `self.rc.accounts.load_lookup_table_addresses_into`, i.e. an accounts-db account load and table deserialization/validation per referenced ALT account [2](#0-1) .

Only after this full parse + ALT-resolution path succeeds and returns a `TransactionState` does `handle_packet_batch_message` call `working_bank.check_transaction_without_status_cache`, which is the point where blockhash age (or nonce) is actually validated [3](#0-2) . `check_transaction_without_status_cache` reads `tx.recent_blockhash()` — a value present directly in the transaction message and independent of any ALT resolution — and looks it up in `hash_queue.get_hash_info_if_valid` [4](#0-3) [5](#0-4) .

Because the account-lock-limit check (`total_num_accounts` vs. `transaction_account_lock_limit`, typically bounding total accounts to the low tens/~64) happens before ALT resolution based on statically-declared lookup counts, an attacker can craft a `V0` message with many distinct ALT accounts (one address per lookup table, maximizing the number of distinct table entries up to the lock limit) and an expired/garbage `recent_blockhash`. Every such packet forces up to the lock-limit number of accounts-db reads and table validations before the cheap blockhash check ever runs and rejects it with `BlockhashNotFound`. The transaction never pays a fee (rejected pre-fee-payer-check, at `check_transaction_without_status_cache`), so this per-packet cost is entirely unpriced.

### Impact Explanation
This is a check-ordering inefficiency that lets an unstaked, unprivileged sender force disproportionate receive-stage CPU/accounts-db work (multiple account loads and table deserializations per packet) for transactions that are provably unschedulable due to blockhash staleness — work that could have been avoided by checking the trivially-cheap blockhash field first. Repeated across a packet burst (up to `PACKET_BURST_LIMIT = 1000` packets per iteration) [6](#0-5) , this amplifies to a meaningful number of wasted accounts-db reads per receive cycle, degrading the leader's receive-stage throughput below its true cost, for zero fee revenue — matching the "grossly underpriced pre-fee work" category.

### Likelihood Explanation
Feasible with just a validly-signed (attacker-controlled keypair, no funds/stake needed) `V0` versioned transaction referencing multiple distinct ALT accounts (real or attacker-created, existence doesn't matter — even `LookupTableAccountNotFound` is only detected during the resolution call itself) and an expired/garbage blockhash. No special permissions, staking, or configuration bypass required; it is repeatable at will by any remote client sending packets to the public TPU port.

### Recommendation
Reorder the checks in `handle_packet_batch_message`/`try_handle_packet` so that the cheap `recent_blockhash`/nonce-age validity check (which only requires the unresolved message data) is performed before `load_addresses_for_view`'s ALT resolution, short-circuiting doomed transactions before incurring accounts-db lookups.

### Proof of Concept
```rust
// core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (test module)
#[test]
fn test_stale_blockhash_with_many_alts_still_resolves_before_age_check() {
    // Build N distinct AddressLookupTableAccount references (N close to
    // transaction_account_lock_limit), pointing at either real ALT accounts
    // pre-seeded in the bank, or nonexistent ones.
    // Set the transaction's recent_blockhash to an expired/garbage hash.
    //
    // Send via receive_and_buffer_packets and instrument/benchmark:
    //   - assert that `load_addresses_from_ref` (or `load_lookup_table_addresses_into`)
    //     is invoked N times per packet (proving resolution happens)
    //   - assert final result is dropped with `TransactionError::BlockhashNotFound`
    //     (num_dropped_on_age == 1, num_buffered == 0)
    //
    // Compare wall-clock/CPU cost of this "N ALTs + stale blockhash" case
    // against a "0 ALTs + stale blockhash" case, and assert the ratio scales
    // linearly with N despite both being guaranteed rejects, demonstrating the
    // check-order allows unpriced amplification proportional to ALT count.
}
```

### Citations

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L159-161)
```rust
        const RECV_TIMEOUT: Duration = Duration::from_millis(10);
        const PACKET_BURST_TIMEOUT: Duration = Duration::from_millis(1);
        const PACKET_BURST_LIMIT: usize = 1000;
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L312-330)
```rust
            // Check blockhash transaction age is ok, or nonce transaction has a valid nonce.
            // Only a fully validated nonce address can be used for priority queue eviction.
            let validated_nonce_address = match working_bank.check_transaction_without_status_cache(
                state.transaction(),
                working_bank.max_processing_age(),
                &mut error_counters,
            ) {
                // Valid nonce transaction
                Ok(Some(nonce_address)) => Some(nonce_address),

                // Valid blockhash transaction
                Ok(None) => None,

                // Invalid
                Err(ref err) => {
                    receiving_stats.add_transaction_error(err);
                    continue;
                }
            };
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L411-455)
```rust
pub(crate) fn translate_to_runtime_view<D: TransactionData>(
    data: D,
    bank: &Bank,
    transaction_account_lock_limit: usize,
    sanitize_config: &SanitizeConfig,
) -> Result<(RuntimeTransaction<ResolvedTransactionView<D>>, u64), PacketHandlingError> {
    // Parsing and basic sanitization checks
    let Ok(view) = SanitizedTransactionView::try_new_sanitized(data, sanitize_config) else {
        return Err(PacketHandlingError::Sanitization);
    };

    let Ok(view) = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
        view,
        MessageHash::Compute,
        None,
    ) else {
        return Err(PacketHandlingError::Sanitization);
    };

    // Discard non-vote packets if in vote-only mode.
    if bank.vote_only_bank() && !view.is_simple_vote_transaction() {
        return Err(PacketHandlingError::Sanitization);
    }

    if usize::from(view.total_num_accounts()) > transaction_account_lock_limit {
        return Err(PacketHandlingError::LockValidation);
    }

    let (loaded_addresses, deactivation_slot) = load_addresses_for_view(&view, bank)?;

    let Ok(view) = RuntimeTransaction::<ResolvedTransactionView<_>>::try_new(
        view,
        loaded_addresses,
        bank.get_reserved_account_keys(),
    ) else {
        return Err(PacketHandlingError::Sanitization);
    };

    // Validate no duplicate accounts (must be after resolution to catch ALT duplicates)
    if validate_account_locks(view.account_keys(), transaction_account_lock_limit).is_err() {
        return Err(PacketHandlingError::LockValidation);
    }

    Ok((view, deactivation_slot))
}
```

**File:** runtime/src/bank/address_lookup_table.rs (L41-68)
```rust
    pub fn load_addresses_from_ref<'a>(
        &self,
        address_table_lookups: impl Iterator<Item = SVMMessageAddressTableLookup<'a>>,
    ) -> Result<(LoadedAddresses, Slot), AddressLoaderError> {
        let slot_hashes = self
            .transaction_processor
            .sysvar_cache()
            .get_slot_hashes()
            .map_err(|_| AddressLoaderError::SlotHashesSysvarNotFound)?;

        let mut deactivation_slot = u64::MAX;
        let mut loaded_addresses = LoadedAddresses::default();
        for address_table_lookup in address_table_lookups {
            deactivation_slot = deactivation_slot.min(
                self.rc
                    .accounts
                    .load_lookup_table_addresses_into(
                        &self.ancestors,
                        address_table_lookup,
                        &slot_hashes,
                        &mut loaded_addresses,
                    )
                    .map_err(into_address_loader_error)?,
            );
        }

        Ok((loaded_addresses, deactivation_slot))
    }
```

**File:** runtime/src/bank/check_transactions.rs (L75-101)
```rust
    pub fn check_transaction_without_status_cache(
        &self,
        tx: &impl SVMMessage,
        max_age: usize,
        error_counters: &mut TransactionErrorMetrics,
    ) -> TransactionResult<Option<Pubkey>> {
        let feature_set: &FeatureSet = &self.feature_set;
        let feature_snapshot = feature_set.snapshot();
        let enable_tx_v1 = feature_snapshot.enable_tx_v1;

        if !enable_tx_v1 && tx.version() == TransactionVersion::Number(1) {
            return Err(TransactionError::UnsupportedVersion);
        }

        let hash_queue = self.blockhash_queue.read().unwrap();
        let next_durable_nonce = hash_queue.next_durable_nonce();

        self.check_transaction_age(
            tx,
            max_age,
            &next_durable_nonce,
            &hash_queue,
            error_counters,
            true, // strict_nonce_size_check
            true, // strict_nonce_authority_check
        )
    }
```

**File:** runtime/src/bank/check_transactions.rs (L229-256)
```rust
    fn check_transaction_age(
        &self,
        tx: &impl SVMMessage,
        max_age: usize,
        next_durable_nonce: &DurableNonce,
        hash_queue: &BlockhashQueue,
        error_counters: &mut TransactionErrorMetrics,
        strict_nonce_size_check: bool,
        strict_nonce_authority_check: bool,
    ) -> TransactionResult<Option<Pubkey>> {
        let recent_blockhash = tx.recent_blockhash();
        if hash_queue
            .get_hash_info_if_valid(recent_blockhash, max_age)
            .is_some()
        {
            Ok(None)
        } else if let Some((nonce_address, _)) = self.check_nonce_transaction_validity(
            tx,
            next_durable_nonce,
            strict_nonce_size_check,
            strict_nonce_authority_check,
        ) {
            Ok(Some(nonce_address))
        } else {
            error_counters.blockhash_not_found += 1;
            Err(TransactionError::BlockhashNotFound)
        }
    }
```
