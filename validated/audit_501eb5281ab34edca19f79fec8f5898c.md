The code path is straightforward and correctly sequenced: `run_transaction_task` (`core/src/sigverify.rs:266-375`) calls `deduper::dedup_packets_and_count_discards` first (marking duplicates as `discard`), then unconditionally runs `sigverify::ed25519_verify_serial` on the *entire remaining batch* — not just a subset believed to be "verified." [1](#0-0) 

`ed25519_verify_serial` (`perf/src/sigverify.rs:127-133`) iterates every packet in the batch and calls `verify_packet` on each one that is not already marked `discard`. Dedup only sets `discard=true` for identified duplicates/false-positives; it never sets a "verified" flag on the survivor. `verify_packet` itself independently checks `packet.meta().discard()` first (returns false to force-discard if already discarded), and otherwise performs full sanitization + signature checks via `SanitizedTransactionView` and `signature.verify(pubkey, message)`. [2](#0-1) [3](#0-2) 

So the "surviving" (non-duplicate) packet is exactly the one that gets passed through `verify_packet` — dedup marks *only* the discarded duplicates, and every packet that is *not* discarded after dedup is still subjected to `ed25519_verify_serial`. There is no code path where "non-discarded" is conflated with "verified" prior to the actual verification call; `discard()` is used purely to skip *already-rejected* packets, and count_valid_packets (`perf/src/sigverify.rs:69-74`) is computed only *after* `ed25519_verify_serial` runs, filtering on the post-verification discard state: [4](#0-3) [5](#0-4) 

The early-return at line 296-298 only triggers when `discard_or_dedup_fail == batch_len`, i.e., when *every* packet (including the "survivor") is discarded — which by definition means there's no undiscarded packet left to leak through. If even a single packet survives dedup, execution continues past that check straight into the priority-floor filter and then unconditionally into `ed25519_verify_serial`, which independently validates the signature regardless of dedup outcome. There is no branch that treats "not discarded by dedup" as equivalent to "signature verified."

The premise in the question — that a code path might exist where dedup survival is mistaken for signature verification — is not supported by the actual control flow. Existing unit tests already validate this invariant, e.g. `test_verify_tampered_sig_len` and `test_verify_medium_fail` in `perf/src/sigverify.rs:497-549`, which construct packets with invalid/tampered signatures and assert they are discarded by verification regardless of their dedup/discard entry state. [6](#0-5) 

#No vulnerability found for this question.

### Citations

**File:** core/src/sigverify.rs (L282-298)
```rust
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

**File:** core/src/sigverify.rs (L326-344)
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

**File:** perf/src/sigverify.rs (L17-63)
```rust
/// Returns true if the signature on the packet verifies.
/// Caller must do packet.set_discard(true) if this returns false.
#[must_use]
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

**File:** perf/src/sigverify.rs (L69-74)
```rust
pub fn count_valid_packets<'a>(batches: impl IntoIterator<Item = &'a PacketBatch>) -> usize {
    batches
        .into_iter()
        .map(|batch| batch.into_iter().filter(|p| !p.meta().discard()).count())
        .sum()
}
```

**File:** perf/src/sigverify.rs (L127-133)
```rust
pub fn ed25519_verify_serial(batch: &mut PacketBatch, reject_non_vote: bool, enable_tx_v1: bool) {
    for mut packet in batch.iter_mut() {
        if !packet.meta().discard() && !verify_packet(&mut packet, reject_non_vote, enable_tx_v1) {
            packet.meta_mut().set_discard(true);
        }
    }
}
```

**File:** perf/src/sigverify.rs (L497-514)
```rust
    #[test]
    fn test_verify_tampered_sig_len() {
        let mut tx = test_tx();
        // pretend malicious leader dropped a signature...
        tx.signatures.pop();
        let packet = BytesPacket::from_data(tx).unwrap();

        let mut batches = generate_packet_batches(&packet, 1, 1);

        // verify packets
        ed25519_verify(&mut batches);
        assert!(
            batches
                .iter()
                .flat_map(|batch| batch.iter())
                .all(|p| p.meta().discard())
        );
    }
```
