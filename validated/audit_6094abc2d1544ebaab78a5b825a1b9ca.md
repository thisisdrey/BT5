### Title
Signature-unverified (sigverify-discarded) packets reach `VoteStorage::insert_packet` because `VotePacketReceiver::buffer_packet_batch` never checks `packet.meta().discard()` - ([File: core/src/banking_stage/vote_packet_receiver.rs])

### Summary
`SigVerifyWorkerPool::run_transaction_task` forwards an entire `PacketBatch` to the banking-stage channel as soon as *any* packet in that batch passes signature verification, without stripping out the other packets that were marked `discard=true` for failing verification. `VotePacketReceiver::buffer_packet_batch` iterates `packet_batch.iter()` and processes every packet without ever checking `packet.meta().discard()`, unlike `forwarding_stage.rs`'s explicit `initial_packet_meta_filter`. This lets an attacker slip crafted, syntactically-valid-but-unverified "vote-shaped" transactions into `vote_storage`.

### Finding Description
On the `tpu_vote` ingress path, `SigVerifyWorkerPool::worker_iteration` calls `run_transaction_task` with `reject_non_vote = true` for the `tpu_vote_receiver` channel [1](#0-0) . Inside `verify_packet`, a packet is only actually signature-checked (and thus can be `verified = true`) if it is shape-recognized as a simple vote transaction; otherwise it is marked `verified = false` (and the caller sets `discard = true`) without ever touching the signature [2](#0-1) .

Crucially, after verification, `run_transaction_task` does not remove/strip the discarded packets from the batch - it only checks that `num_valid_packets` (i.e. at least one valid packet) is non-zero before forwarding the *entire* `batch`, discarded packets included, to the banking-stage channel: [3](#0-2) 

Downstream, `VotePacketReceiver::buffer_packet_batch` (reached via `VoteWorker::run` -> `tpu_receiver.receive_and_buffer_packets`) iterates every packet in that batch with no `discard()` check whatsoever: [4](#0-3) 

It re-parses raw bytes with `SanitizedTransactionView::try_new_sanitized` (structural sanitization only, no signature check), applies `should_filter_packet` (a deny-list of pubkeys, unrelated to vote-ness or signatures) [5](#0-4) , and then calls `vote_storage.insert_packet`, which only verifies that the first instruction bincode-deserializes into an expected `VoteInstruction` variant — it never re-checks signatures [6](#0-5) [7](#0-6) .

This is in stark contrast to `ForwardingStage::buffer_packet_batches`, which explicitly filters on `initial_packet_meta_filter(p.meta())` (which checks discard) before doing anything else [8](#0-7) . `VotePacketReceiver` has no equivalent guard, so any packet in a batch that was marked `discard=true` for failing sigverify (unverified signature, or shape rejected as non-vote) is still fully processed and, if it happens to parse as a `VoteInstruction`, is accepted into `vote_storage`.

Exploit flow: an unstaked attacker sends a burst of packets to the leader's `tpu_vote` QUIC/UDP port within a coalescing window such that they land in the same `PacketBatch`: (1) one genuinely valid, correctly-signed vote transaction (trivially obtainable by replaying any validator's publicly gossiped/broadcast vote transaction bytes, requiring no private key), and (2) additional crafted packets whose signatures are unverified/invalid but whose instruction data deserializes to a valid `VoteInstruction` (e.g. `TowerSync`). Because packet (1) makes `num_valid_packets > 0`, the whole batch — including the unverified packet(s) (2) — is forwarded to the `tpu_vote` banking channel and ultimately inserted into `vote_storage` without ever having a verified signature.

### Impact Explanation
This breaks the invariant that every transaction reaching banking/vote storage has passed signature verification. Unverified, attacker-forged "vote" data enters `VoteStorage` and is later consumed by `VoteWorker`/`Consumer` as if it were a legitimately signed vote, corresponding to the Agave bounty category of sanitization/signature-verification bypass on the ingress path.

### Likelihood Explanation
The precondition (bundling a replayed, genuinely valid vote packet with forged packets into the same batch sent to the public `tpu_vote` port) is fully within reach of an unstaked remote attacker — no operator/staked/gossip control is required, and validator votes are broadcast plaintext data anyone can capture and replay. The bug is deterministic given the code paths shown (missing discard filter is a structural code gap, not a probabilistic race).

### Recommendation
Add an explicit discard filter in `VotePacketReceiver::buffer_packet_batch`, mirroring `forwarding_stage.rs`'s `initial_packet_meta_filter`, e.g. `packet_batch.iter().filter(|p| !p.meta().discard())`, before any further processing, ensuring only sigverify-passed packets reach `vote_storage.insert_packet`.

### Proof of Concept
Integration test plan (Rust, in `core/src/banking_stage/vote_packet_receiver.rs` test module):
1. Build a `PacketBatch` containing two packets: `packet_valid` — a real, correctly signed `TowerSync` vote transaction (as in `packet_from_slots`), and `packet_forged` — a syntactically valid transaction whose single instruction bincode-deserializes as `VoteInstruction::TowerSync` but has an invalid/garbage signature.
2. Manually mark `packet_forged.meta_mut().set_discard(true)` to simulate what `sigverify::verify_packet` would produce (since `reject_non_vote`/signature check fails for it), while leaving `packet_valid` undiscarded — reproducing exactly the batch shape that `run_transaction_task` would forward per lines 332-356 of `core/src/sigverify.rs`.
3. Send this batch through `VotePacketReceiver::receive_and_buffer_packets` directly (bypassing the sigverify stage, since the vulnerable code is downstream of it).
4. Assert: `vote_storage.len() == 2` (or that `packet_forged`'s vote pubkey appears in storage), proving the discarded/unverified packet was inserted despite never having a verified signature — expected/fixed behavior after the recommended patch is `vote_storage.len() == 1`.

### Citations

**File:** core/src/sigverify.rs (L239-251)
```rust
            recv(&channels.tpu_vote_receiver) -> maybe_work => {
                match maybe_work {
                    Ok(batch) => Self::run_transaction_task(
                        batch,
                        true,
                        &channels.forward_stage_sender,
                        true,
                        true,
                        &channels.sharable_banks,
                        &channels.tpu_vote_state,
                    ),
                    Err(_) => false,
                }
```

**File:** core/src/sigverify.rs (L332-356)
```rust
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

        let banking_packet_batch = BankingPacketBatch::new(batch);
        // Sample backlog before the push: measures consumer health without
        // including this batch's own contribution.
        state
            .stats
            .max_pre_send_len
            .fetch_max(state.banking_stage_sender.len(), Ordering::Relaxed);
        match state
            .banking_stage_sender
            .send(banking_packet_batch.clone())
        {
```

**File:** perf/src/sigverify.rs (L30-63)
```rust
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

**File:** core/src/banking_stage/vote_packet_receiver.rs (L136-149)
```rust
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
```

**File:** core/src/banking_stage/vote_packet_receiver.rs (L222-229)
```rust
    fn should_filter_packet(&self, packet: &SanitizedTransactionView<Bytes>) -> bool {
        // Vote transactions do not use address lookup tables, so static keys cover this path.
        !self.filter_keys.is_empty()
            && packet
                .static_account_keys()
                .iter()
                .any(|key| self.filter_keys.contains(key))
    }
```

**File:** core/src/banking_stage/vote_storage.rs (L108-120)
```rust
    pub(crate) fn insert_packet(
        &mut self,
        vote_source: VoteSource,
        packet: SanitizedTransactionView<Bytes>,
    ) -> VoteInsertionMetrics {
        let Ok(vote) =
            LatestValidatorVote::new_from_view(packet, vote_source, self.deprecate_legacy_vote_ixs)
        else {
            return VoteInsertionMetrics::default();
        };

        self.insert_vote(vote, false)
    }
```

**File:** core/src/banking_stage/latest_validator_vote_packet.rs (L39-79)
```rust
        let (_, instruction) = vote
            .program_instructions_iter()
            .next()
            .ok_or(DeserializedPacketError::VoteTransaction)?;

        let instruction_filter = |ix: &VoteInstruction| {
            if deprecate_legacy_vote_ixs {
                matches!(
                    ix,
                    VoteInstruction::TowerSync(_) | VoteInstruction::TowerSyncSwitch(_, _),
                )
            } else {
                ix.is_single_vote_state_update()
            }
        };

        match limited_deserialize::<VoteInstruction>(instruction.data, PACKET_DATA_SIZE as u64) {
            Ok(vote_state_update_instruction)
                if instruction_filter(&vote_state_update_instruction) =>
            {
                let ix_key = |offset| {
                    let index = instruction
                        .accounts
                        .get(offset)
                        .copied()
                        .ok_or(DeserializedPacketError::VoteTransaction)?;
                    let pubkey = vote
                        .static_account_keys()
                        .get(index as usize)
                        .copied()
                        .ok_or(DeserializedPacketError::VoteTransaction)?;
                    let signed = index < vote.num_required_signatures();

                    Ok((pubkey, signed))
                };

                let (vote_pubkey, _) = ix_key(0)?;
                let (authorized_voter_pubkey, authorized_voter_signed) = ix_key(1)?;
                if !authorized_voter_signed {
                    return Err(DeserializedPacketError::VoteTransaction);
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
