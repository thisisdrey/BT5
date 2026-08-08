### Title
Unbounded per-packet CPU spent resolving multiple address-lookup-tables before cheap sanitization fails - free amplification via `translate_to_runtime_view` - (File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs)

### Summary
`translate_to_runtime_view` orders its checks so that the cheap `total_num_accounts` vs. `transaction_account_lock_limit` check only bounds the *sum of resolved indices*, not the *number of distinct address-lookup-table (ALT) entries* referenced in a v0 message. An attacker can craft a single sigverify-valid, unfunded transaction that references many real/valid ALT accounts (each contributing few or zero indices) followed by one final invalid lookup, forcing the bank to perform many real accounts-db reads/deserializations in `load_addresses_for_view`/`Bank::load_addresses_from_ref` before finally failing with `PacketHandlingError::ALTResolution`, at zero cost to the sender.

### Finding Description
In `translate_to_runtime_view` [1](#0-0) , the sequence is:
1. Cheap parse/sanitize via `SanitizedTransactionView::try_new_sanitized` and `RuntimeTransaction::try_new` (structural checks only, using `sanitize_config()` limits built from `MAX_INSTRUCTION_TRACE_LENGTH`/`MAX_ACCOUNTS_PER_INSTRUCTION`) [2](#0-1) .
2. A cheap check that `view.total_num_accounts() <= transaction_account_lock_limit` [3](#0-2) . This check operates purely on the *unresolved* view (static keys + declared writable/readonly index counts from each `MessageAddressTableLookup`), not on the number of distinct ALT lookup entries.
3. Only after that cheap gate does `load_addresses_for_view` call `bank.load_addresses_from_ref(...)`, which iterates every `address_table_lookup` entry and calls `self.rc.accounts.load_lookup_table_addresses_into(...)` for each one — a real accounts-db read plus ALT-state deserialization and index-bounds validation per entry [4](#0-3) [5](#0-4) .

Because a v0 message's `MessageAddressTableLookup` entry can have empty `writable_indexes`/`readonly_indexes` vectors (compact-array length 0) while still carrying a real ALT pubkey, an attacker can pack many small lookup entries (~34 bytes each: 32-byte pubkey + 2 length bytes) into a single packet, referencing the same well-known, large, existing ALT account repeatedly (or several distinct existing ALTs). None of these contribute meaningfully to `total_num_accounts`, so the cheap early check passes trivially. The loop in `Bank::load_addresses_from_ref` uses `?` to short-circuit on the *first* failing entry [6](#0-5) , so by placing all valid entries first and a single invalid lookup (nonexistent account or out-of-range index) last, the attacker forces the bank to perform accounts-db lookups and deserialize the ALT state for every prior valid entry before the transaction is finally rejected with `PacketHandlingError::ALTResolution`.

Because failure happens inside `translate_to_runtime_view`/`load_addresses_for_view`, the packet never reaches `check_fee_payer_unlocked` or fee/lock validation [7](#0-6) , so the sender pays nothing — the transaction only needs a syntactically valid signature (self-signed, unfunded keypair suffices) to pass sigverify and reach this code path.

### Impact Explanation
This allows an unstaked remote attacker who can only get packets past QUIC ingress and sigverify to impose disproportionate, real accounts-db read/deserialize work on the banking-stage's ALT-resolution path per packet, for zero fee, since the packet is rejected before any fee-payer check or cost/fee accounting occurs. This is a pre-fee CPU-amplification / underpriced-work issue in the transaction ingestion pipeline, matching the "grossly underpriced pre-fee work" bounty category referenced in the question, since the number of real ALT lookups performed (bounded only by packet size, roughly dozens per 1232-byte packet) is not gated by the cheap `total_num_accounts` check that runs first.

### Likelihood Explanation
The only precondition is the ability to send arbitrary well-formed (but ultimately invalid) v0 transactions to a leader's TPU, and knowledge of a few widely used, large, existing ALT accounts (which are public on-chain data, e.g., any popular DEX or program's lookup table). No stake, funds, or special access is required, and the attack is trivially repeatable per packet/connection at the rate normal transaction traffic is accepted, since no per-packet cost is charged until well after this resolution step.

### Recommendation
Reorder/strengthen the cheap-rejection gate in `translate_to_runtime_view` so the number of distinct `address_table_lookups` entries (not just the summed resolved index count) is bounded before any accounts-db work begins, and/or make `Bank::load_addresses_from_ref` bail out (or count against a fixed lookup-table budget) as soon as the number of resolved ALT entries processed exceeds a small constant tied to realistic legitimate use, independent of whether later entries would ultimately succeed. Consider validating structural bounds (max number of `address_table_lookups` entries) during the cheap `SanitizedTransactionView::try_new_sanitized` stage, before any bank/accounts-db interaction occurs.

### Proof of Concept
Rust integration test plan (in `core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs` test module or a new benchmark):
1. Set up a `Bank` with N (e.g., 30) distinct, real, populated `AddressLookupTable` accounts created via the address-lookup-table program (or reuse of accounts-db test helpers), each large enough to require nontrivial deserialization (e.g., near max ALT size).
2. Construct transaction A: minimal v0 message, single instruction, one ALT lookup referencing one *invalid* (nonexistent) ALT pubkey placed first — expect `PacketHandlingError::ALTResolution` after essentially one lookup.
3. Construct transaction B: same shape, but with N-1 `address_table_lookup` entries each referencing one of the N valid ALT accounts (with empty or minimal writable/readonly indexes) followed by one final invalid lookup entry, sign with a throwaway keypair with zero lamports.
4. Call `translate_to_runtime_view` (or `translate_transaction`) directly for both A and B against the test bank, measuring wall-clock/CPU time or accounts-db read counters.
5. Assert that transaction B takes significantly more accounts-db reads/CPU time than transaction A despite both ultimately returning `Err(PacketHandlingError::ALTResolution)` and both being "free" (no fee ever charged, `check_fee_payer_unlocked` never invoked) — demonstrating the cost is proportional to attacker-controlled ALT-entry count rather than bounded independent of it, violating the "sanitization must reject cheaply before expensive resolution" invariant.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L332-340)
```rust
            // Check the transaction's fee-payer validates.
            if let Err(_err) = Consumer::check_fee_payer_unlocked(
                working_bank,
                state.transaction(),
                &mut error_counters,
            ) {
                receiving_stats.num_dropped_on_fee_payer += 1;
                continue;
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

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L459-472)
```rust
pub(crate) fn load_addresses_for_view<D: TransactionData>(
    view: &SanitizedTransactionView<D>,
    bank: &Bank,
) -> Result<(Option<LoadedAddresses>, Slot), PacketHandlingError> {
    match view.version() {
        TransactionVersion::Legacy | TransactionVersion::V1 => Ok((None, u64::MAX)),
        TransactionVersion::V0 => bank
            .load_addresses_from_ref(view.address_table_lookup_iter())
            .map(|(loaded_addresses, deactivation_slot)| {
                (Some(loaded_addresses), deactivation_slot)
            })
            .map_err(|_| PacketHandlingError::ALTResolution),
    }
}
```

**File:** runtime-transaction/src/sanitize_config.rs (L13-21)
```rust
/// Returns the [`SanitizeConfig`] with current protocol limits.
pub fn sanitize_config() -> SanitizeConfig {
    SanitizeConfig {
        min_requested_heap_size: MIN_HEAP_FRAME_BYTES,
        max_requested_heap_size: MAX_HEAP_FRAME_BYTES,
        max_instructions: MAX_INSTRUCTION_TRACE_LENGTH,
        max_accounts_per_instruction: MAX_ACCOUNTS_PER_INSTRUCTION,
    }
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
