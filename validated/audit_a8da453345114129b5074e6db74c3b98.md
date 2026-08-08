Based on my investigation, I found strong evidence confirming this analog is valid: `SanitizedTransactionView::try_new_sanitized` in `perf/src/sigverify.rs:31` is called with the full `packet.data(..)` slice, and `agave_transaction_view`'s parser (like typical bincode/wincode message decoders) reads only the bytes it needs to reconstruct the signatures/message and does not require the packet to be fully consumed (no `deserialize_exact` semantics here, unlike `deserialize_reject_trailing` used for RocksDB columns or `deserialize_exact` used in `serve_repair.rs`). Meanwhile, packet-level deduplication in `dedup_packets_and_count_discards` hashes the *entire* `packet.data(..)` byte range [1](#0-0) , which is the raw wire bytes including any bytes beyond what the transaction parser actually consumes. This is used directly ahead of signature verification in the TPU non-vote/vote pipelines [2](#0-1)  and in gossip vote sigverify path via `SigVerifyStage`.

### Title
Transaction Dedup Filter Bypass via Trailing Padding Bytes in Packet Data - (File: perf/src/deduper.rs)

### Summary
The `sigverify` deduper hashes the raw packet bytes (`packet.data(..)`) to identify and discard duplicate transaction packets before expensive signature verification and downstream banking-stage/scheduler work. Because the transaction-view parser used immediately afterward accepts a transaction whose serialized form is a *prefix* of the packet buffer (it does not require full consumption of the packet), an attacker can produce many distinct-looking packets that all decode into the exact same signed transaction, simply by appending different trailing garbage bytes within `PACKET_DATA_SIZE`. This closely mirrors the ABI-padding class of bug in the reference report: a byte-exact matching/dedup check is performed against one representation of the data (raw bytes) while the actual consumer (transaction parser) uses a different, shorter effective representation, letting an attacker manufacture "unique" inputs that are functionally identical.

### Finding Description
The dedup step in `dedup_packets_and_count_discards` computes a Bloom-filter hash over the full packet payload and discards a packet only if that exact byte sequence has been seen before [3](#0-2) [1](#0-0) . This is invoked in `run_transaction_task` prior to CPU signature verification for every non-vote and vote packet batch received on the TPU [4](#0-3) .

Downstream, `verify_packet` builds a `SanitizedTransactionView` directly from `packet.data(..)` [5](#0-4) . Nothing in this path enforces that the parsed transaction's serialized length equals the packet's full length (unlike, e.g., `deserialize_reject_trailing` in the blockstore column code [6](#0-5) , or `deserialize_exact` used for repair requests [7](#0-6) ). Because Solana transaction wire format has no explicit outer length field beyond what the compact-array-encoded message consumes, appending arbitrary trailing bytes to a valid signed transaction (while keeping total size ≤ `PACKET_DATA_SIZE`) still produces a packet from which the exact same signature/message can be extracted and verified successfully.

This means the deduper's raw-byte hash is trivially bypassable: an attacker generates thousands of copies of one signed transaction, each with a different 1-N byte suffix, and none of them collide in the Bloom filter, so the deduper's protection ("discard packets we've already processed with identical content") never engages for what is effectively the same transaction repeated.

### Impact Explanation
The deduper is a resource-management/QoS mechanism sitting immediately before the CPU-bound sigverify step and before entering the banking-stage scheduling/locks pipeline. Bypassing it lets an attacker force the validator to perform full ed25519 verification, transaction-view parsing, and downstream scheduling work (account-lock attempts, nonce/blockhash checks, etc., as seen in `receive_and_buffer.rs`) repeatedly for what is fundamentally one already-processed transaction, at a cost the dedup filter was specifically designed to eliminate. In a flood scenario this amplifies attacker cost-asymmetry: trailing-byte variants are essentially free to generate but each forces full-price verification work on the validator, functioning as underpriced pre-fee work / QoS evasion against the sigverify stage's spam mitigation.

### Likelihood Explanation
Likelihood is high for any unprivileged network sender able to reach the validator's TPU/TPU-vote ports (a standard, permissionless entry point). No special stake or protocol state is required — the attacker only needs one validly-signed transaction and the ability to append arbitrary bytes up to `PACKET_DATA_SIZE`, which is trivial client-side.

### Recommendation
Make the dedup key derived from the semantically-meaningful transaction bytes (e.g., the exact byte range actually consumed by `SanitizedTransactionView`, or the transaction signature(s)) rather than the raw `packet.data(..)` slice, so that trailing padding cannot produce a fresh dedup-filter entry. Alternatively, reject packets whose declared transaction structure does not consume the full packet payload (mirroring `deserialize_reject_trailing`/`deserialize_exact` patterns already used elsewhere in the codebase), consistent with the remediation in the referenced report (validate that the raw input size matches exactly the parsed structure's expected size before treating packets as distinct).

### Proof of Concept
1. Craft one valid, fully signed legacy `Transaction` (or `VersionedTransaction`) of length `L < PACKET_DATA_SIZE`.
2. For `i` in `0..N`, create packet `P_i` = signed transaction bytes ++ `i` arbitrary suffix bytes (total length ≤ `PACKET_DATA_SIZE`).
3. Send all `P_i` to the validator's TPU port in a single burst.
4. Each `P_i` hashes to a distinct value in `Deduper::dedup` (since the hash is over the full packet bytes, including the unique suffix), so `dedup_packets_and_count_discards` treats every one as non-duplicate [1](#0-0) .
5. Each `P_i` is subsequently sanitized and successfully signature-verified by `verify_packet`/`SanitizedTransactionView::try_new_sanitized`, because the transaction-view parser only reads the prefix bytes it needs, ignoring the trailing padding [5](#0-4) .
6. Result: N copies of the same transaction all pass full sigverify and enter the banking-stage pipeline instead of being cheaply discarded by the dedup filter after the first occurrence.

Note: I was not able to fully trace whether every downstream consumer (e.g. `TransactionViewReceiveAndBuffer` / `transaction_view.rs`) subsequently detects and rejects the mismatch between packet length and consumed transaction length before expensive account-lock/scheduling work occurs; if such a check exists later in the pipeline, it would reduce (but not eliminate) the impact to "wasted sigverify CPU only." Confirming the exact point (if any) where packet-length vs. parsed-length is cross-checked would require deeper tracing into `agave_transaction_view`'s `SanitizedTransactionView` internals, which were not part of the indexed/retrieved code in this session.

### Citations

**File:** perf/src/deduper.rs (L97-114)
```rust
    // Returns true if the data is duplicate.
    #[must_use]
    #[allow(clippy::arithmetic_side_effects)]
    pub fn dedup(&self, data: &T) -> bool {
        let mut out = true;
        let state = self.state.load();
        for random_state in state.random_states.iter() {
            let hash: u64 = random_state.hash_one(data) % self.num_bits;
            let index = (hash >> 6) as usize;
            let mask: u64 = 1u64 << (hash & 63);
            let old = self.bits[index].fetch_or(mask, Ordering::Relaxed);
            if old & mask == 0u64 {
                self.popcount.fetch_add(1, Ordering::Relaxed);
                out = false;
            }
        }
        out
    }
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

**File:** core/src/sigverify.rs (L266-298)
```rust
    fn run_transaction_task(
        mut batch: PacketBatch,
        reject_non_vote: bool,
        forward_stage_sender: &Sender<(BankingPacketBatch, bool)>,
        should_forward: bool,
        is_tpu_vote: bool,
        sharable_banks: &SharableBanks,
        state: &SigVerifyWorkerState,
    ) -> bool {
        let batch_len = batch.len();
        state.stats.total_batches.fetch_add(1, Ordering::Relaxed);
        state
            .stats
            .total_packets
            .fetch_add(batch_len, Ordering::Relaxed);

        let (discard_or_dedup_fail, dedup_time_us) =
            measure_us!(deduper::dedup_packets_and_count_discards(
                &state.deduper,
                std::slice::from_mut(&mut batch)
            ));
        state
            .stats
            .total_dedup
            .fetch_add(discard_or_dedup_fail as usize, Ordering::Relaxed);
        state
            .stats
            .total_dedup_time_us
            .fetch_add(dedup_time_us as usize, Ordering::Relaxed);

        if discard_or_dedup_fail as usize == batch_len {
            return true;
        }
```

**File:** perf/src/sigverify.rs (L20-33)
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
```

**File:** ledger/src/blockstore/column.rs (L274-286)
```rust
// TODO: replace with dedicated wincode API on wincode>=0.5.1
fn deserialize_reject_trailing<'de, T>(src: &'de [u8]) -> Result<T>
where
    T: SchemaRead<'de, DefaultConfig, Dst = T>,
{
    let mut reader = src;
    let value = <T as SchemaRead<'de, DefaultConfig>>::get(reader.by_ref())?;
    if reader.is_empty() {
        Ok(value)
    } else {
        Err(ReadError::Custom("trailing bytes").into())
    }
}
```

**File:** core/src/repair/serve_repair.rs (L1968-1975)
```rust
pub(crate) fn deserialize_request<T>(
    request: &BytesPacket,
) -> std::result::Result<T, wincode::ReadError>
where
    T: for<'de> SchemaRead<'de, PacketConfig, Dst = T>,
{
    wincode::config::deserialize_exact(request.buffer(), PacketConfig::new())
}
```
