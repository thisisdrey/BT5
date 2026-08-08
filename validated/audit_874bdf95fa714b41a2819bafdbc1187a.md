### Title
Per-connection (not aggregate) unstaked stream throttle lets many distinct unstaked IPs collectively flood the bounded `packet_batch_sender` channel, starving staked traffic - (File: `streamer/src/nonblocking/stream_throttle.rs`)

### Summary
The QUIC streamer's unstaked stream throttle (`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration`) grants each individual unstaked *connection* its own quota (`max_unstaked_load_in_throttling_window`, derived from `MAX_UNSTAKED_TPS = 200`), rather than enforcing that quota in aggregate across all unstaked connections. Because `DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000` and `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8` allow many distinct unstaked IPs to each open several fully-compliant connections, the aggregate unstaked stream/packet rate the leader will accept and forward into the bounded `packet_batch_sender` crossbeam channel can reach orders of magnitude above the intended ~200 TPS unstaked budget, causing the shared channel to backpressure and delay/drop legitimate staked packets downstream.

### Finding Description
`ConnectionStreamCounter` is created per-connection (`streamer/src/nonblocking/swqos.rs:218`, inside `cache_new_connection`) and stored in `SwQosConnectionContext.stream_counter`. Each time a stream is accepted, `on_new_stream` (`streamer/src/nonblocking/swqos.rs:497-516`) calls `throttle_stream` (`streamer/src/nonblocking/stream_throttle.rs:233-271`) using that connection's own counter and a quota returned by `max_streams_per_throttling_interval` → `available_load_capacity_in_throttling_duration` (`streamer/src/nonblocking/stream_throttle.rs:167-188`).

For `ConnectionPeerType::Unstaked` this returns a fixed constant, `max_unstaked_load_in_throttling_window`, computed once at server construction as:
```
max_unstaked_load_in_throttling_window = MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
                                        = 200 * 100 / 1000 = 20
```
(`streamer/src/nonblocking/stream_throttle.rs:17,21,64-68`). This quota (20 streams per 100 ms, i.e. 200 streams/sec) is applied **independently per connection**, because each connection has its own `ConnectionStreamCounter` and `reset_throttling_params_if_needed` resets that counter on its own timer (`streamer/src/nonblocking/stream_throttle.rs:213-230`). There is no shared/global counter across unstaked connections that enforces the intended aggregate 200 TPS unstaked budget — the `EMA` (`current_load_ema`) tracked in `StakedStreamLoadEMA` is only incremented for **staked** traffic (`increment_load`, `streamer/src/nonblocking/stream_throttle.rs:160-165`: `if peer_type.is_staked() { ... }`), so unstaked load never feeds back into any global control loop at all.

Combined with admission limits `DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000` and `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8` (`streamer/src/quic.rs:41,48`), an attacker who is unprivileged/unstaked can:
1. Open connections from many distinct IPs (each individually complying with `max_connections_per_unstaked_peer`, the per-IP connection-rate limiter, and the global connection-rate limiter in `run_server`, `streamer/src/nonblocking/quic.rs:270-281,346-369`).
2. Fill the global unstaked connection table up to `max_unstaked_connections` (2000).
3. On every connection, open streams at the max allowed per-connection rate (200 streams/sec), each carrying a minimally-sized packet.
4. Because throttling is per-connection, the aggregate unstaked ingestion rate scales linearly with connection count: up to `2000 * 200 = 400,000` packets/sec, all pushed via `handle_chunk`'s `packet_sender.try_send(packet_batch)` into the shared bounded `packet_batch_sender` channel (`streamer/src/nonblocking/quic.rs:816`), rather than the intended ~200 TPS unstaked ceiling.

None of the existing guards stop this:
- Per-IP connection rate limiter and global connection rate limiter only bound *connection establishment*, not per-connection stream/packet rate.
- `max_connections_per_unstaked_peer` bounds connections per IP, not aggregate throughput across many IPs.
- The stream throttle intended to cap unstaked throughput operates per-connection, not in aggregate, so it does not prevent horizontal scaling of the attack across IPs.
- The staked/unstaked split in `packet_batch_sender` stats (`total_unstaked_packets_sent_for_batching` / `total_staked_packets_sent_for_batching`, `streamer/src/nonblocking/quic.rs:841-852`) shows the channel is shared — unstaked traffic saturating it directly backpressures staked packet delivery (`TrySendError::Full`, `streamer/src/nonblocking/quic.rs:816-825`).

### Impact Explanation
This is a QoS-evasion / unprivileged capacity-starvation issue: unstaked (fee-uncommitted or minimally-fee'd) traffic that individually respects every stated per-connection/per-IP limit can still consume a disproportionate, effectively unbounded (scaling with IP count) share of the leader's bounded packet-batch ingestion channel. This directly violates the invariant that "work spent per packet before a fee is collected is bounded and proportionate" and causes legitimate staked senders' packets to be delayed or dropped in banking stage due to channel backpressure, i.e., a resource-starvation/DoS impact on TPU capacity for staked traffic.

### Likelihood Explanation
Preconditions are exactly the default configuration values named in the question (`DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000`, `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8`) — no operator misconfiguration is required. The attack requires only distributing connections across many source IPs (a purely network-layer requirement, well within reach of a botnet or cloud-IP pool), each IP independently satisfying the per-IP rate/connection limits. This is entirely reproducible and repeatable in a local integration test using multiple bound sockets with distinct loopback-alias or spoofed source addresses feeding into `setup_quic_server`/`spawn_stake_weighted_qos_server`.

### Recommendation
Enforce the unstaked stream/packet quota in aggregate rather than per-connection: maintain a single shared token-bucket/EMA counter across all unstaked connections (mirroring how `current_load_ema` already aggregates staked load in `StakedStreamLoadEMA`), and have `throttle_stream` consult/consume from that shared budget for `ConnectionPeerType::Unstaked` instead of (or in addition to) the per-connection `ConnectionStreamCounter`. Additionally consider directly rate-limiting/bounding the fraction of the `packet_batch_sender` channel capacity reserved for unstaked traffic (e.g., separate bounded channels or a reserved slot count for staked vs unstaked) so that the shared channel cannot be starved by unstaked backpressure regardless of connection count.

### Proof of Concept
Integration test plan (extends existing tests such as `test_quic_server_multiple_streams` in `streamer/src/nonblocking/quic.rs:1632`):
1. Spawn a `spawn_stake_weighted_qos_server` with defaults (`max_unstaked_connections: 2000`, `max_connections_per_unstaked_peer: 8`, small bounded `packet_batch_sender` e.g. `bounded(1024)`) and one staked peer.
2. Simulate N distinct unstaked "IPs" (using multiple ports/sockets bound to distinct loopback aliases, e.g. `127.0.0.2..127.0.0.250`) each opening up to 8 connections and continuously opening uni-streams with 1-byte payloads at max allowed per-connection rate.
3. Concurrently, have a single staked client attempt to send transactions through the same server.
4. Assert: `stats.total_handle_chunk_to_packet_send_full_err` (channel-full errors, `streamer/src/nonblocking/quic.rs:820-825`) rises sharply, and the staked client's packet delivery latency/throughput (measured via `total_staked_packets_sent_for_batching` vs elapsed time) degrades significantly compared to a baseline run without the concurrent unstaked flood — demonstrating that per-connection throttling, unlike the intended aggregate 200 unstaked-TPS budget, fails to prevent unstaked traffic from starving the shared `packet_batch_sender` channel.