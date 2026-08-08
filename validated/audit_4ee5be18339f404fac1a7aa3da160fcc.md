### Title
Missing discard-flag check in `VotePacketReceiver::buffer_packet_batch` allows sigverify-failed vote packets into `vote_storage` - (File: core/src/banking_stage/vote_packet_receiver.rs)

### Summary
`SigVerifyWorkerPool::run_transaction_task` verifies signatures per-packet but forwards the *entire batch* to banking stage as long as at least one packet in the batch passed verification, leaving individually-discarded (invalid-signature) packets still present in the forwarded `BankingPacketBatch`. `VotePacketReceiver::buffer_packet_batch`, unlike `ForwardingStage::buffer_packet_batches`, never checks `packet.meta().discard()` before parsing and inserting the packet into `VoteStorage`, so a vote-shaped packet whose signature failed verification can still be buffered and reach the vote processing / PoH pipeline.

### Finding Description
In `perf/src/sigverify.rs::verify_packet` (lines 20-63), for a packet that is classified as a simple vote transaction (`is_simple_vote_transaction_view` true), signature verification is executed via `signatures.iter().zip(...).all(|(signature, pubkey)| signature.verify(...))`; if the signature is garbage, `verified` is `false`. The caller, `ed25519_verify_serial` / `ed25519_verify` (lines 108-133), sets `packet.meta_mut().set_discard(true)` on this individual packet — it does **not** remove the packet from the batch.

In `core/src/sigverify.rs::SigVerifyWorkerPool::run_transaction_task` (lines 266-344), after `ed25519_verify_serial` runs, the code computes `num_valid_packets` for the *whole batch* and only drops the *entire* batch if `num_valid_packets == 0` (line 342-344). If any other packet in the same batch passed verification (a plausible occurrence given batches are built from `recv_mmsg`-style socket reads that can coalesce packets from multiple senders), the full batch — including the discarded, invalid-signature vote packet — is wrapped in `BankingPacketBatch::new(batch)` and sent to `state.banking_stage_sender` (lines 346-369), which is wired to `tpu_vote_receiver` / `gossip_vote_receiver` consumed by `VotePacketReceiver` (`core/src/banking_stage.rs::spawn_vote_worker`, lines 611-616).

`VotePacketReceiver::buffer_packet_batch` (`core/src/banking_stage/vote_packet_receiver.rs`, lines 125-167) then iterates `packet_batch.iter()` and, for every packet, immediately calls `packet.data(..)` and `SanitizedTransactionView::try_new_sanitized(...)` followed by `vote_storage.insert_packet(vote_source, packet)` — **without ever checking `packet.meta().discard()`**. This is in contrast to the analogous non-vote path, `ForwardingStage::buffer_packet_batches` (`core/src/forwarding_stage.rs`, lines 270-286), which explicitly does `.filter(|p| initial_packet_meta_filter(p.meta()))` before touching packet data, and even documents the invariant with an `unreachable!()` guard for discarded packets.

Because `PacketRef::data(..)` does not itself gate on the discard flag (confirmed by the fact that other call sites such as `perf/src/deduper.rs::dedup_packets_and_count_discards` and `ledger/src/sigverify_shreds.rs::verify_shred_cpu` explicitly check `packet.meta().discard()` before calling `.data(..)`), `SanitizedTransactionView::try_new_sanitized` in `buffer_packet_batch` will happily parse and structurally sanitize a discarded packet's raw bytes and hand it to `vote_storage.insert_packet`, which performs no independent signature check (signature checking is solely delegated to the earlier sigverify stage).

### Impact Explanation
An unstaked attacker can craft a well-formed single-instruction vote transaction targeting `solana_sdk_ids::vote::id()` with a garbage/unrelated signature and send it to the leader's TPU vote port. If it lands in a batch alongside at least one legitimately-signed vote packet (plausible under normal cluster vote traffic, since batches are constructed from socket reads that mix packets from many senders), the individually-discarded (invalid-signature) packet is not filtered out downstream and is inserted into `VoteStorage`/`vote_storage` via `VotePacketReceiver::buffer_packet_batch`. This is a signature-verification-bypass finding: unauthenticated data reaches the vote processing pipeline that is meant to only contain sig-verified vote transactions, violating the stated invariant that "signature verification is applied uniformly regardless of vote/non-vote classification" before any per-packet buffering decision downstream of sigverify.

### Likelihood Explanation
Reaching the vulnerable code path requires only network access to the TPU vote endpoint (unprivileged) and crafting a structurally valid single-instruction vote transaction with an arbitrary signature — trivial and fully attacker-controlled. The only non-trivial precondition is that the attacker's packet must be co-batched with at least one packet that passes sigverify, which is a function of normal traffic/batching timing rather than any privileged capability, making this feasible and repeatable, especially during periods of high vote traffic.

### Recommendation
Add an explicit `packet.meta().discard()` check in `VotePacketReceiver::buffer_packet_batch` (mirroring `ForwardingStage::buffer_packet_batches`'s `initial_packet_meta_filter`) before calling `packet.data(..)`/`SanitizedTransactionView::try_new_sanitized`, so that any packet marked discarded by sigverify is skipped rather than parsed and inserted into `VoteStorage`.

### Proof of Concept
```rust
// core/src/banking_stage/vote_packet_receiver.rs (add to `mod tests`)
#[test]
fn test_receive_and_buffer_skips_discarded_packet() {
    let keypairs = ValidatorVoteKeypairs::new_rand();
    // Build a structurally valid vote packet, then corrupt its signature bytes.
    let mut vote_packet = packet_from_slots(vec![(1, 1)], &keypairs, None);
    // Simulate what sigverify does to an invalid-signature vote packet:
    vote_packet.meta_mut().set_discard(true);

    let (sender, receiver) = bounded(1024);
    sender
        .send(Arc::new(PacketBatch::from(vec![vote_packet])))
        .unwrap();

    let mut receiver = VotePacketReceiver::new(receiver, Arc::new(HashSet::new()));
    let genesis_config =
        genesis_utils::create_genesis_config_with_vote_accounts(100, &[keypairs], vec![200])
            .genesis_config;
    let (bank, _bank_forks) = Bank::new_with_bank_forks_for_tests(&genesis_config);
    let mut vote_storage = VoteStorage::new(&bank);
    let mut banking_stage_stats = BankingStageStats::new();
    let mut slot_metrics_tracker = LeaderSlotMetricsTracker::default();

    receiver
        .receive_and_buffer_packets(
            &mut vote_storage,
            &mut banking_stage_stats,
            &mut slot_metrics_tracker,
            VoteSource::Tpu,
        )
        .unwrap();

    // Expected (after fix): discarded packet must not be buffered.
    assert_eq!(vote_storage.len(), 0);
}
```
Currently this assertion fails (`vote_storage.len() == 1`), demonstrating that `buffer_packet_batch` ignores the discard flag set by sigverify. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** perf/src/sigverify.rs (L20-63)
```rust
fn verify_packet(packet: &mut PacketRefMut, reject_non_vote: bool, enable_tx_v1: bool) -> bool {
    // If this packet was already marked as discard, drop it
    if packet.meta().discard() {
        return false;
    }

    let Some(data) = packet.data(..) else {
        return false;
    };

    let (is_simple_vote_tx, verified) = {
        let Ok(view) = SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()) else {
            return false;
        };

        if !enable_tx_v1 && matches!(view.version(), TransactionVersion::V1) {
            return false;
        }

        let is_simple_vote_tx = is_simple_vote_transaction_view(&view);
        if reject_non_vote && !is_simple_vote_tx {
            (is_simple_vote_tx, false)
        } else {
            let signatures = view.signatures();
            if signatures.is_empty() {
                (is_simple_vote_tx, false)
            } else {
                let message = view.message_data();
                let static_account_keys = view.static_account_keys();
                let verified = signatures
                    .iter()
                    .zip(static_account_keys.iter())
                    .all(|(signature, pubkey)| signature.verify(pubkey.as_ref(), message));
                (is_simple_vote_tx, verified)
            }
        }
    };

    if is_simple_vote_tx {
        packet.meta_mut().flags |= PacketFlags::SIMPLE_VOTE_TX;
    }

    verified
}
```

**File:** perf/src/sigverify.rs (L108-133)
```rust
pub fn ed25519_verify(
    thread_pool: &rayon::ThreadPool,
    batches: &mut [PacketBatch],
    reject_non_vote: bool,
    packet_count: usize,
    enable_tx_v1: bool,
) {
    debug!("CPU ECDSA for {packet_count}");
    thread_pool.install(|| {
        batches.par_iter_mut().flatten().for_each(|mut packet| {
            if !packet.meta().discard()
                && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1)
            {
                packet.meta_mut().set_discard(true);
            }
        });
    });
}

pub fn ed25519_verify_serial(batch: &mut PacketBatch, reject_non_vote: bool, enable_tx_v1: bool) {
    for mut packet in batch.iter_mut() {
        if !packet.meta().discard() && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1) {
            packet.meta_mut().set_discard(true);
        }
    }
}
```

**File:** core/src/sigverify.rs (L326-345)
```rust
        let enable_tx_v1 = working_bank.feature_set.snapshot().enable_tx_v1;
        let (_, verify_time_us) = measure_us!(sigverify::ed25519_verify_serial(
            &mut batch,
            reject_non_vote,
            enable_tx_v1,
        ));
        let num_valid_packets = sigverify::count_valid_packets(std::iter::once(&batch));
        state
            .stats
            .total_valid_packets
            .fetch_add(num_valid_packets, Ordering::Relaxed);
        state
            .stats
            .total_verify_time_us
            .fetch_add(verify_time_us as usize, Ordering::Relaxed);

        if num_valid_packets == 0 {
            return true;
        }

```

**File:** core/src/banking_stage/vote_packet_receiver.rs (L125-167)
```rust
    fn buffer_packet_batch(
        &self,
        packet_batch: &BankingPacketBatch,
        sanitize_config: &SanitizeConfig,
        vote_storage: &mut VoteStorage,
        vote_source: VoteSource,
        slot_metrics_tracker: &mut LeaderSlotMetricsTracker,
        stats: &mut ReceiveAndBufferStats,
    ) {
        stats.num_packets_received += packet_batch.len();

        for packet in packet_batch.iter() {
            let Some(packet_data) = packet.data(..) else {
                continue;
            };

            match SanitizedTransactionView::try_new_sanitized(
                packet_bytes(packet, packet_data),
                sanitize_config,
            ) {
                Ok(packet) => {
                    if self.should_filter_packet(&packet) {
                        stats.packet_stats.filtered_account_key_count += 1;
                        continue;
                    }

                    stats.num_buffered_packets += 1;
                    let vote_insertion_metrics = vote_storage.insert_packet(vote_source, packet);
                    slot_metrics_tracker.accumulate_vote_insertion_metrics(&vote_insertion_metrics);
                    stats.dropped_packets_count += vote_insertion_metrics.total_dropped_packets();
                }
                Err(err) => {
                    stats.errors += 1;
                    match err {
                        TransactionViewError::AddressLookupMismatch => {}
                        TransactionViewError::ParseError | TransactionViewError::SanitizeError => {
                            stats.packet_stats.failed_sanitization_count += 1
                        }
                    }
                }
            }
        }
    }
```

**File:** core/src/forwarding_stage.rs (L270-286)
```rust
    fn buffer_packet_batches(
        &mut self,
        packet_batch: BankingPacketBatch,
        is_tpu_vote_batch: bool,
        bank: &Bank,
    ) {
        let sanitize_config = sanitize_config();
        for packet in packet_batch
            .iter()
            .filter(|p| initial_packet_meta_filter(p.meta()))
        {
            let Some(packet_data) = packet.data(..) else {
                unreachable!(
                    "packet.meta().discard() was already checked. If not discarded, packet MUST \
                     have data"
                );
            };
```

**File:** core/src/banking_stage.rs (L611-616)
```rust
    fn spawn_vote_worker(&self) -> JoinHandle<()> {
        let vote_storage = VoteStorage::new(&self.bank_forks.read().unwrap().working_bank());
        let tpu_receiver =
            VotePacketReceiver::new(self.tpu_vote_receiver.clone(), self.filter_keys.clone());
        let gossip_receiver =
            VotePacketReceiver::new(self.gossip_vote_receiver.clone(), self.filter_keys.clone());
```

**File:** perf/src/deduper.rs (L125-144)
```rust
pub fn dedup_packets_and_count_discards<const K: usize>(
    deduper: &Deduper<K, [u8]>,
    batches: &mut [PacketBatch],
) -> u64 {
    batches
        .iter_mut()
        .flat_map(|batch| batch.iter_mut())
        .map(|mut packet| {
            if !packet.meta().discard()
                && packet
                    .data(..)
                    .map(|data| deduper.dedup(data))
                    .unwrap_or(true)
            {
                packet.meta_mut().set_discard(true);
            }
            u64::from(packet.meta().discard())
        })
        .sum()
}
```

**File:** ledger/src/sigverify_shreds.rs (L20-56)
```rust
#[must_use]
pub fn verify_shred_cpu(
    packet: PacketRef,
    slot_leaders: &SlotPubkeys,
    cache: &RwLock<LruCache>,
) -> bool {
    if packet.meta().discard() {
        return false;
    }
    let Some(shred) = shred::layout::get_shred(packet) else {
        return false;
    };
    let Some(slot) = shred::layout::get_slot(shred) else {
        return false;
    };
    trace!("slot {slot}");
    let Some(pubkey) = slot_leaders.get(&slot) else {
        return false;
    };
    let Some(signature) = shred::layout::get_signature(shred) else {
        return false;
    };
    trace!("signature {signature}");
    let Some(data) = shred::layout::get_merkle_root(shred) else {
        return false;
    };

    let key = (signature, *pubkey, data);
    if cache.read().unwrap().get(&key).is_some() {
        true
    } else if key.0.verify(key.1.as_ref(), key.2.as_ref()) {
        cache.write().unwrap().put(key, ());
        true
    } else {
        false
    }
}
```
