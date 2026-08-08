### Title
Unauthenticated UDP senders are unconditionally trusted as "from staked node," causing QoS/staked-only filters to be evaded - ([File: core/src/fetch_stage.rs])

### Summary
The external report describes a case where an untrusted, attacker-controlled field (`l1QueueOrigin`) is accepted at face value instead of being validated against the real origin of the transaction, letting an attacker relabel traffic to change how downstream logic treats it. The closest reachable analog in Agave is the `PacketFlags::FROM_STAKED_NODE` classification applied to packets received on the legacy UDP TPU-vote socket: the flag is set unconditionally to `true` for every packet arriving on that socket, with no actual verification that the sender holds stake, yet downstream code (e.g. forwarding admission) trusts this flag as a proxy for "verified staked sender."

### Finding Description
When the `FetchStage` is constructed for the legacy UDP TPU-vote listener, it spawns `streamer::receiver` for each vote socket with the `is_staked_service` parameter hard-coded to `true`: [1](#0-0) 

That flag flows into `streamer::recv_loop`, which stamps every packet received on the socket with `PacketFlags::FROM_STAKED_NODE` regardless of who actually sent it: [2](#0-1) 

Unlike the QUIC stake-weighted admission path (`spawn_stake_weighted_qos_server` in `core/src/tpu.rs`), which cryptographically authenticates the sender via the QUIC TLS client certificate and cross-references a `StakedNodes` table before setting this flag, the plain UDP vote socket has no client authentication at the transport layer at all — any host on the network can send arbitrary UDP datagrams to this port. The code nonetheless treats every packet as if it came from a "staked" (trusted) peer.

Downstream, `PacketFlags::FROM_STAKED_NODE` is used as a hard gate for further propagation of the packet. In `forwarding_stage.rs`, `initial_packet_meta_filter` only allows staked-flagged, non-discarded, non-forwarded packets into the buffer that will subsequently be relayed to the leader: [3](#0-2) [4](#0-3) 

Because the flag is set unconditionally on the UDP path rather than validated against the actual identity/stake of the sender, this is structurally the same class of bug as the reported issue: a classification field that downstream logic relies on for correctness/trust decisions is accepted without verifying it reflects the real origin of the data, allowing anyone to make traffic appear as though it came from a trusted/staked class of sender.

### Impact Explanation
Any unprivileged, unstaked party can send UDP traffic to a validator's TPU-vote port and have it unconditionally tagged as `FROM_STAKED_NODE`. This traffic then passes the staked-only admission check in `forwarding_stage.rs` that is specifically designed to filter out unstaked/untrusted senders before packets are relayed onward to the current leader. This is a QoS-evasion vector: unstaked traffic bypasses the mechanism meant to restrict forwarding/relaying to verified, stake-weighted senders, and validators end up acting as amplifiers/relays for unauthenticated traffic under the label of trusted staked traffic. Actual acceptance into the block still depends on real vote-account stake checks elsewhere (e.g. `VoteStorage`), so this does not directly forge state, but it undermines a QoS control gate whose entire purpose is to distinguish staked from unstaked senders before that point in the pipeline.

### Likelihood Explanation
High from a reachability standpoint: the legacy UDP TPU-vote socket is expected to be reachable by external senders (it's the direct ingestion path for votes), requires no cryptographic handshake, and the `is_staked_service = true` hard-coding is unconditional for all packets on that socket — there is no code path that verifies actual stake for UDP-sourced vote packets before setting this flag.

### Recommendation
Do not hard-code `is_staked_service = true` for the plain UDP TPU-vote receiver. Either (a) remove reliance on `FROM_STAKED_NODE` for downstream trust decisions when the packet arrived over an unauthenticated transport, (b) perform real stake verification (e.g., by validating the transaction's vote-account stake at receive time before setting the flag), or (c) restrict/deprecate the unauthenticated UDP vote-ingestion path in favor of the QUIC stake-weighted server, where the flag is set based on actual verified identity.

### Proof of Concept
Not independently reproduced in this analysis (no execution/test environment available). The reasoning is based on static code inspection:
1. `FetchStage::new_multi_socket` in `core/src/fetch_stage.rs` spawns UDP receivers for the TPU-vote socket with `is_staked_service` hard-coded `true`.
2. `streamer::recv_loop` in `streamer/src/streamer.rs` sets `PacketFlags::FROM_STAKED_NODE` on every packet from that socket based solely on this hard-coded flag.
3. `forwarding_stage.rs`'s `initial_packet_meta_filter` uses this flag as the sole gate deciding whether a packet is eligible to be forwarded onward.
4. An external, unstaked sender crafting a UDP datagram (even garbage bytes shaped to survive initial parsing, or a real vote-shaped transaction from an unstaked keypair) directed at the TPU-vote UDP port would have its packet marked `FROM_STAKED_NODE` and pass the forwarding admission filter, despite not being a verified staked sender.

**Uncertainty note:** I could not fully trace whether additional downstream checks (e.g., real per-vote-account stake lookups in `VoteStorage`/`insert_packets`) fully neutralize the practical impact before any consensus-relevant effect occurs, nor could I confirm from static search whether the UDP TPU-vote socket is enabled/reachable by default in current deployment configurations (some clusters may only enable the QUIC listener). This would need to be confirmed in a live/test environment, which is unavailable in this indexed, read-only analysis.

### Citations

**File:** core/src/fetch_stage.rs (L183-199)
```rust
        let tpu_vote_threads: Vec<_> = tpu_vote_sockets
            .into_iter()
            .enumerate()
            .map(|(i, socket)| {
                streamer::receiver(
                    format!("solRcvrTpuVot{i:02}"),
                    socket,
                    exit.clone(),
                    vote_sender.clone(),
                    recycler.clone(),
                    tpu_vote_stats.clone(),
                    coalesce,
                    true,
                    true, // only staked connections should be voting
                )
            })
            .collect();
```

**File:** streamer/src/streamer.rs (L215-217)
```rust
                    packet_batch
                        .iter_mut()
                        .for_each(|p| p.meta_mut().set_from_staked_node(is_staked_service));
```

**File:** core/src/forwarding_stage.rs (L270-292)
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

            let vote_count = usize::from(is_tpu_vote_batch);
            let non_vote_count = usize::from(!is_tpu_vote_batch);

            self.metrics.votes_received += vote_count;
            self.metrics.non_votes_received += non_vote_count;
```

**File:** core/src/forwarding_stage.rs (L768-770)
```rust
fn initial_packet_meta_filter(meta: &packet::Meta) -> bool {
    !meta.discard() && !meta.forwarded() && meta.is_from_staked_node()
}
```
