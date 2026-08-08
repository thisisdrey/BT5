### Title
Fetch Stage hardcodes `is_staked_service=true` for tpu_vote UDP socket, letting unstaked attackers spoof staked-origin packet metadata - ([File: core/src/fetch_stage.rs])

### Summary
`FetchStage::new_multi_socket` calls `streamer::receiver(...)` for every `tpu_vote_sockets` entry with the last argument hardcoded to `true` (comment: "only staked connections should be voting"), but this socket is a plain UDP socket with no cryptographic identity check on senders. Inside `streamer::recv_loop`, every packet pulled from that socket unconditionally gets `p.meta_mut().set_from_staked_node(is_staked_service)` applied, so any UDP packet reaching this port is marked as staked-origin regardless of actual sender identity.

### Finding Description
`FetchStage::new_multi_socket` in `core/src/fetch_stage.rs` spawns a receiver thread per `tpu_vote_sockets` entry via `streamer::receiver(..., true, true)`, where the final `true` is passed as `is_staked_service` with the comment "only staked connections should be voting" [1](#0-0) . This value flows into `recv_loop` in `streamer/src/streamer.rs`, which for every packet received on the socket executes `packet_batch.iter_mut().for_each(|p| p.meta_mut().set_from_staked_node(is_staked_service));` — with no verification of sender identity, stake, or any cryptographic signature [2](#0-1) .

Because `tpu_vote_sockets` are plain `UdpSocket`s bound to a public port (not the QUIC-based staked-connection stack with actual stake-weighted authentication used elsewhere), any unprivileged remote client can send an arbitrary UDP datagram to this port. `recv_from`/`recv_mmsg` reads the datagram with no identity/authentication check, and the hardcoded `true` causes the packet's metadata flag `is_from_staked_node` to be set to `true` unconditionally — exactly matching packets that traveled through a genuinely staked, authenticated QUIC connection elsewhere in the codebase (e.g., `streamer/src/nonblocking/quic.rs`, which sets this flag based on actual verified stake).

### Impact Explanation
Downstream consumers (e.g. `core/src/forwarding_stage.rs`, which reads `is_from_staked_node`) use this flag to distinguish staked vs. unstaked traffic for QoS/prioritization purposes [3](#0-2) . An unstaked attacker's forged vote-port packets are indistinguishable from genuine staked vote traffic once they reach this code path, which can let attacker traffic bypass any unstaked-specific throttling/deprioritization intended for the vote-processing path, unfairly consuming vote packet-processing capacity — a QoS/prioritization evasion.

### Likelihood Explanation
This is trivially and repeatably reachable by any unprivileged remote UDP client: no stake, no valid vote, no cryptographic material is required — a raw UDP packet to the leader's public `tpu_vote` port hits `recv_loop` and unconditionally is marked staked, every time.

### Recommendation
Do not hardcode `is_staked_service=true` for the plain-UDP `tpu_vote` socket path in `FetchStage::new_multi_socket`. Either determine staked status from the actual sender's verified identity, or set `is_staked_service=false` for this unauthenticated UDP socket and apply appropriate unstaked throttling instead.

### Proof of Concept
Integration test plan:
1. Spawn a `FetchStage`-style `streamer::receiver` bound to a UDP socket with `is_staked_service=true` (mirroring `new_multi_socket`'s call for `tpu_vote_sockets`).
2. From a separate unstaked test socket (no identity, no stake, no gossip/registration), send a raw garbage UDP datagram to the receiver's port.
3. Read the resulting `PacketBatch` off the returned channel.
4. Assert `packet.meta().is_from_staked_node() == true`, despite the sending socket having no stake relationship whatsoever — demonstrating the classification bypass at `streamer/src/streamer.rs:217` driven by the hardcoded flag in `core/src/fetch_stage.rs:196`.

### Citations

**File:** core/src/fetch_stage.rs (L186-197)
```rust
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
```

**File:** streamer/src/streamer.rs (L215-217)
```rust
                    packet_batch
                        .iter_mut()
                        .for_each(|p| p.meta_mut().set_from_staked_node(is_staked_service));
```

**File:** core/src/forwarding_stage.rs (L1-1)
```rust
//! `ForwardingStage` is a stage parallel to `BankingStage` that forwards
```
