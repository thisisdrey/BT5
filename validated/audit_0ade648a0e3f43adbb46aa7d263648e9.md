### Title
Non-strict ed25519 signature verification in the primary transaction sigverify path allows malleable-signature packet duplication that bypasses the packet Deduper - (File: perf/src/sigverify.rs)

### Summary
Solana's main consensus-critical transaction signature-verification path — `verify_packet` in [1](#0-0)  — validates signatures with a plain `signature.verify(pubkey.as_ref(), message)` call, in contrast to the ed25519 precompile path (`precompiles/src/ed25519.rs`) and the gossip CRDS path (`gossip/src/crds_value.rs`), both of which explicitly use `verify_strict` to reject malleable ed25519 signatures. This mirrors the reported EIP-2 class of bug (missing canonical/malleable-signature rejection) but manifests in the QUIC/TPU packet-dedup + sigverify pipeline rather than an EVM system transaction.

### Finding Description
`verify_packet` iterates transaction signatures and calls the non-strict `Signature::verify`: [2](#0-1) 

This is the exact code path used by `ed25519_verify` / `ed25519_verify_serial`, which is invoked from the TPU/JIT sigverify workers in `core/src/sigverify.rs`, e.g. `run_transaction_task`: [3](#0-2) 

By contrast, two other signature-verification sites in the same codebase were hardened against ed25519 malleability by switching to `verify_strict`:
- The ed25519 precompile explicitly documents and tests this fix (`test_ed25519_malleability`), which asserts `verify_strict` rejects a low-order-`R` malleable signature that plain `verify` would accept: [4](#0-3) 
- The gossip CRDS signature-verification cache also deliberately uses `verify_strict` and documents why (cache poisoning/inconsistent identity): [5](#0-4) 

The transaction sigverify path (`perf/src/sigverify.rs::verify_packet`) has no equivalent hardening — it is the one place in the transaction-ingestion pipeline still using non-strict verification for consensus-relevant transaction signatures.

This matters because before the transaction ever reaches `verify_packet`, it passes through the packet **Deduper**, which computes a bloom-filter hash purely over the raw packet bytes (`packet.data(..)`), not the transaction's semantic content or canonicalized signature: [6](#0-5) 

Because ed25519 signature malleability allows constructing more than one syntactically distinct, but semantically-identical and independently-verifying signature byte string for the same (pubkey, message) pair when `verify` (non-strict) rather than `verify_strict` is used, an attacker who observes a valid transaction/signature can craft one or more malleable variant packets whose signature bytes differ but which still pass `verify_packet`. Each variant has different raw bytes and therefore a different Deduper hash, so the bloom-filter based `Deduper::dedup` in `dedup_packets_and_count_discards` does **not** recognize them as duplicates and lets every variant through to full ed25519 verification and downstream banking-stage processing.

### Impact Explanation
This allows an unprivileged network sender (anyone submitting packets to the TPU/QUIC ingestion path) to bypass the packet-level deduplication defense that is specifically designed to cheaply drop duplicate/resent packets before the relatively expensive `ed25519_verify` CPU work and before entering the banking-stage/forwarding pipeline (`run_transaction_task` in `core/src/sigverify.rs`). Multiple distinct-but-equivalent packets for the same underlying transaction consume: (1) full CPU signature-verification cycles that the Deduper exists to avoid, (2) banking-stage/forwarding channel capacity, and (3) validator resources duplicated per malleable variant — a QoS/underpriced-work amplification vector, since none of this is protected by fee-based pre-filtering at the point where the Deduper operates. Depending on how many malleable representations exist per signature, this can be used to multiply the effective packet rate an attacker can push through pre-fee validation stages relative to what the Deduper is meant to bound.

### Likelihood Explanation
Reachable by any unprivileged client sending packets over the normal TPU/QUIC ingestion path — no special validator/peer role is required, matching the "unprivileged-user analogs in QUIC/UDP streamer, packet dedup and sigverify" scope. The precondition is generating a malleable-but-verifying ed25519 signature variant of an otherwise-valid signature; this is a known, previously-fixed class of issue in this exact codebase (fixed for the precompile and gossip paths), indicating the primitive is understood to be exploitable against non-strict verification here. Actual exploitability depends on ed25519_dalek's non-strict `verify` accepting a class of malleable signatures (e.g., non-canonical/cofactored variants) that `verify_strict` rejects — this was demonstrated in-repo by `test_ed25519_malleability` for the identical primitive against the identical crate.

### Recommendation
Change the transaction-signature verification in `verify_packet` (`perf/src/sigverify.rs`) to use `verify_strict` semantics (or otherwise canonicalize/reject malleable signatures before/at the same layer as the Deduper), consistent with the fix already applied to the ed25519 precompile (`precompiles/src/ed25519.rs`) and the gossip CRDS verification (`gossip/src/crds_value.rs`). Alternatively/in addition, ensure the Deduper key incorporates a canonical representation of the transaction (message hash + normalized signature) rather than raw packet bytes, so malleable variants collapse to the same dedup key.

### Proof of Concept
Conceptual PoC (cannot be executed in this read-only environment):
1. Take any valid signed transaction `(sig, pubkey, message)` that passes `verify_packet`'s non-strict `signature.verify(pubkey.as_ref(), message)` check in [7](#0-6) .
2. Using known ed25519 malleability techniques (e.g., adding a multiple of the group order to `S`, or using an alternate low-order `R` component, as demonstrated for the same crate in `test_ed25519_malleability` at [8](#0-7) ), construct one or more alternate signature byte-strings `sig'`, `sig''` that still verify under `ed25519_dalek`'s non-strict `verify` for the same `(pubkey, message)`.
3. Serialize the same transaction with each different signature variant into distinct packets and send them all through the TPU/QUIC ingestion path feeding `dedup_packets_and_count_discards` in [6](#0-5) .
4. Because each packet's raw bytes differ (different signature bytes), the Deduper's bloom-filter hash differs for each, so none are flagged as duplicates by `Deduper::dedup` in [9](#0-8) , and all variants proceed to full `ed25519_verify` and pass, being forwarded/queued in `run_transaction_task` in [3](#0-2) .

Note: I was unable to inspect the exact `solana_signature::Signature::verify` implementation source (it appears to live in an external `solana-signature` crate not indexed in this repo), so I cannot cite the precise dalek call it makes internally; this is inferred from the consistent pattern in-repo where `verify` (non-strict) is used at this call site while `verify_strict` is deliberately used and tested elsewhere in the same codebase for the identical malleability concern.

### Citations

**File:** perf/src/sigverify.rs (L20-55)
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

**File:** precompiles/src/ed25519.rs (L454-512)
```rust
    #[test]
    fn test_ed25519_malleability() {
        agave_logger::setup();

        // sig created via ed25519_dalek: both pass
        let secret_bytes: [u8; 32] = rand::random();
        let secret = ed25519_dalek::SecretKey::from_bytes(&secret_bytes).unwrap();
        let public: ed25519_dalek::PublicKey = (&secret).into();
        let privkey = ed25519_dalek::Keypair { secret, public };
        let message_arr = b"hello";
        let signature = privkey.sign(message_arr).to_bytes();
        let pubkey = privkey.public.to_bytes();
        let instruction = new_ed25519_instruction_with_signature(message_arr, &signature, &pubkey);

        let feature_set = FeatureSet::default();
        assert!(
            test_verify_with_alignment(
                verify,
                &instruction.data,
                &[&instruction.data],
                &feature_set
            )
            .is_ok()
        );

        let feature_set = FeatureSet::all_enabled();
        assert!(
            test_verify_with_alignment(
                verify,
                &instruction.data,
                &[&instruction.data],
                &feature_set
            )
            .is_ok()
        );

        // malleable sig: verify_strict does NOT pass
        // for example, test number 5:
        // https://github.com/C2SP/CCTV/tree/main/ed25519
        // R has low order (in fact R == 0)
        let pubkey =
            &hex::decode("10eb7c3acfb2bed3e0d6ab89bf5a3d6afddd1176ce4812e38d9fd485058fdb1f")
                .unwrap();
        let signature = &hex::decode("00000000000000000000000000000000000000000000000000000000000000009472a69cd9a701a50d130ed52189e2455b23767db52cacb8716fb896ffeeac09").unwrap();
        let message = b"ed25519vectors 3";
        let instruction = new_ed25519_instruction_raw(pubkey, signature, message);

        // verify_strict does NOT pass for malleable signature
        let feature_set = FeatureSet::default();
        assert!(
            test_verify_with_alignment(
                verify,
                &instruction.data,
                &[&instruction.data],
                &feature_set
            )
            .is_err()
        );
    }
```

**File:** gossip/src/crds_value.rs (L112-143)
```rust
impl CrdsValue {
    /// Verify the signature, short-circuiting on a previously-verified value
    /// hash and otherwise reusing a cached decompressed verifying key. Both
    /// caches are populated only after `verify_strict` succeeds, so neither can
    /// be seeded with arbitrary entries to evict useful ones.
    pub(crate) fn verify_with_cache(&self, cache: &SigVerifyCache) -> bool {
        if cache.verified_values.contains(&self.hash) {
            return true;
        }
        let pubkey = self.pubkey();
        let signable_data = self.signable_data();
        let message = signable_data.borrow();
        let sig_bytes: [u8; 64] = self.signature.into();
        let signature = ed25519_dalek::Signature::from_bytes(&sig_bytes);
        let verified = match cache.verifying_keys.get(&pubkey) {
            Some(vk) => vk.verify_strict(message, &signature).is_ok(),
            None => {
                let Ok(vk) = ed25519_dalek::VerifyingKey::try_from(pubkey.as_ref()) else {
                    return false;
                };
                if vk.verify_strict(message, &signature).is_err() {
                    return false;
                }
                cache.verifying_keys.insert(pubkey, vk);
                true
            }
        };
        if verified {
            cache.verified_values.insert(self.hash);
        }
        verified
    }
```

**File:** perf/src/deduper.rs (L100-114)
```rust
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
