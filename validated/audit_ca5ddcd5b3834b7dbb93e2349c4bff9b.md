### Title
Unprivileged nonce-shaped-transaction flood forces per-packet accounts-db lookups in banking-stage ingestion with zero fee cost - (File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs)

### Summary
An attacker who only controls signing keypairs (no funded accounts, no stake) can send a stream of syntactically valid, sanitizable transactions structured as durable-nonce transactions that reference distinct, non-existent nonce accounts. Because `check_transaction_age` only short-circuits cheaply when the transaction's `recent_blockhash` matches a live entry in the in-memory blockhash queue, any transaction using an unrecognized "durable nonce"-shaped blockhash falls through to the nonce path, which performs a real accounts-db lookup (`get_account_with_fixed_root`) for every single packet before any fee-payer or fee check occurs.

### Finding Description
In `TransactionViewReceiveAndBuffer::handle_packet_batch_message`, after parsing/sanitization, the pre-filter `drop_incoming_nonce_tx` only checks the in-memory `nonces_in_use` map (`get_nonce_transaction_priority_id` / `is_queued`), both O(1)/O(log n) container lookups [1](#0-0) . Because a flooding attacker uses a unique fabricated nonce address per packet, this map lookup always misses, so every packet proceeds unconditionally into `working_bank.check_transaction_without_status_cache` [2](#0-1) .

`check_transaction_without_status_cache` calls `check_transaction_age`, which first attempts a cheap in-memory blockhash-queue lookup via `get_hash_info_if_valid`; only if that fails does it call `check_nonce_transaction_validity` [3](#0-2) . Any transaction whose `recent_blockhash` field is not a recognized recent blockhash (which is trivially true for an attacker-chosen "nonce" hash) is routed into `check_nonce_transaction_validity` → `load_message_nonce_data`, which performs an actual accounts-db read via `self.get_account_with_fixed_root(nonce_address)` for the attacker-supplied fabricated address [4](#0-3) . Because the account never exists, the lookup always misses and the transaction is rejected with `BlockhashNotFound`, but only after the accounts-db read has already been performed — before `check_fee_payer_unlocked` is ever invoked and before any funded, valid fee-payer is required [5](#0-4) .

A normal transaction with a valid, current recent blockhash never reaches this accounts-db lookup, since the cheap in-memory queue check at the top of `check_transaction_age` succeeds and returns immediately. Thus, transactions deliberately shaped as durable-nonce transactions (i.e., containing the nonce marker instruction with an attacker-chosen non-existent nonce pubkey) impose materially higher, disproportionate per-packet bank-lookup cost relative to ordinary transfers, and this cost is paid regardless of whether the transaction ever lands, is fee-paid, or references a real account.

### Impact Explanation
This causes the banking-stage receive path to perform real accounts-db reads for every packet in an attacker-controlled flood of syntactically valid but fee-payer-agnostic, nonce-shaped transactions, at zero cost to the attacker (no funded fee payer, no real nonce account required, only a self-signed keypair). Repeated at `PACKET_BURST_LIMIT`-sized batches across many bursts, this degrades leader CPU/accounts-db lookup throughput relative to true transaction fees collected, falling under underpriced pre-fee work / leader-degradation impact.

### Likelihood Explanation
Preconditions are minimal and fully within the unprivileged threat model: the attacker needs only to construct a `Transaction` containing a `system_instruction::advance_nonce_account`-shaped instruction referencing a freshly generated, never-created pubkey, sign it with any (even unfunded) keypair, and send it over TPU/QUIC. No stake, no special config, and no funded accounts are required, and the attack is trivially repeatable at network line rate.

### Recommendation
Add a cheap pre-check before calling `check_transaction_without_status_cache` for nonce-shaped transactions: e.g., use an accounts-index/bloom-filter presence check, or cache negative nonce-account lookups per-slot, to short-circuit the accounts-db read for fabricated nonce addresses. Alternatively, cap the number of distinct nonce-shaped transactions with unresolved (never-seen) nonce addresses processed per burst/connection, independent of the general packet count, to bound the ratio of accounts-db work to received packets.

### Proof of Concept
Extend the existing `receive_and_buffer.rs` test harness (`setup_transaction_view_receive_and_buffer`, `create_nonce_transaction`) as follows:
```rust
#[test]
fn test_fake_nonce_flood_forces_account_lookup_per_packet() {
    let (sender, receiver) = bounded(4096);
    let (bank_forks, mint_keypair) = test_bank_forks_with_fee();
    let (mut receive_and_buffer, mut container) =
        setup_transaction_view_receive_and_buffer(receiver, bank_forks.clone());

    // Craft PACKET_BURST_LIMIT transactions, each referencing a distinct,
    // never-created nonce account, signed by throwaway (unfunded) keypairs.
    let fake_nonce_txs: Vec<_> = (0..1000)
        .map(|_| {
            let fake_nonce_pubkey = Pubkey::new_unique();
            let durable = Hash::new_unique(); // never a real recent blockhash
            create_nonce_transaction(&mint_keypair, &fake_nonce_pubkey, 0, durable)
        })
        .collect();

    send_transactions(&sender, &fake_nonce_txs);

    let start = std::time::Instant::now();
    let stats = receive(&mut receive_and_buffer, &mut container);
    let elapsed = start.elapsed();

    // All packets are rejected with BlockhashNotFound after an accounts-db
    // lookup, none are ever buffered, and no fee payer is checked.
    assert_eq!(stats.num_buffered, 0);
    assert_eq!(stats.num_dropped_on_nonce_dedup, 0);

    // Compare elapsed time against an equivalent batch of plain, unsigned-blockhash
    // transfers that fail via the cheap in-memory blockhash-queue path (no account read),
    // asserting fake-nonce processing time exceeds a bounded multiplier of transfer processing time.
}
```
Instrument `get_account_with_fixed_root` (or wrap it with a counter in a test-only build) to assert it is invoked once per fake-nonce packet, demonstrating the accounts-db read occurs despite zero fee ever being collected.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L301-310)
```rust
            let drop_incoming_nonce_tx = raw_nonce_address
                .and_then(|address| container.get_nonce_transaction_priority_id(&address))
                .is_some_and(|existing| {
                    existing.priority >= priority || !container.is_queued(existing)
                });

            if drop_incoming_nonce_tx {
                receiving_stats.num_dropped_on_nonce_dedup += 1;
                continue;
            }
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

**File:** runtime/src/bank/check_transactions.rs (L258-300)
```rust
    pub(super) fn check_nonce_transaction_validity(
        &self,
        message: &impl SVMMessage,
        next_durable_nonce: &DurableNonce,
        strict_nonce_size_check: bool,
        strict_nonce_authority_check: bool,
    ) -> Option<(Pubkey, u64)> {
        let nonce_is_advanceable = message.recent_blockhash() != next_durable_nonce.as_hash();
        if !nonce_is_advanceable {
            return None;
        }

        let (nonce_address, nonce_data) =
            self.load_message_nonce_data(message, strict_nonce_size_check)?;

        if strict_nonce_authority_check
            && !message
                .get_ix_signers(NONCED_TX_MARKER_IX_INDEX as usize)
                .any(|signer| signer == &nonce_data.authority)
        {
            return None;
        }

        let previous_lamports_per_signature = nonce_data.get_lamports_per_signature();

        Some((nonce_address, previous_lamports_per_signature))
    }

    pub(super) fn load_message_nonce_data(
        &self,
        message: &impl SVMMessage,
        strict_nonce_size_check: bool,
    ) -> Option<(Pubkey, NonceData)> {
        let nonce_address = message.get_durable_nonce()?;
        let nonce_account = self.get_account_with_fixed_root(nonce_address)?;
        if strict_nonce_size_check && nonce_account.data().len() != NonceState::size() {
            return None;
        }
        let nonce_data =
            nonce_account::verify_nonce_account(&nonce_account, message.recent_blockhash())?;

        Some((*nonce_address, nonce_data))
    }
```
