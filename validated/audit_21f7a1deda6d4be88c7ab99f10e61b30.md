### Title
Handshake-stalling attacker can saturate `max_concurrent_connections` and block all new TPU connections - ([File: streamer/src/nonblocking/quic.rs])

### Summary
`ClientConnectionTracker::new` increments the global `open_connections` counter and gates `tasks.spawn(setup_connection)` against `qos.max_concurrent_connections()` *before* the QUIC handshake completes, and the slot is only released after `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) elapses via the `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` in `setup_connection`. An attacker who opens `max_concurrent_connections` connections and never completes the handshake occupies every slot for up to 2 seconds, causing all further `Incoming` connections (staked or unstaked) to be refused with `refused_connections_too_many_open_connections` regardless of per-IP/per-peer QoS.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:304-411`), for every incoming QUIC `Incoming`, the code:
1. Applies `overall_connection_rate_limiter` and per-IP `rate_limiter` checks [1](#0-0) .
2. Calls `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())`, which does `fetch_add` on `stats.open_connections` and compares against the global bound before any handshake data has been validated [2](#0-1) [3](#0-2) .
3. If under the bound, it calls `incoming.accept()` and `tasks.spawn(setup_connection(...))` [4](#0-3) .

Inside `setup_connection`, the handshake future is awaited with `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting)` — `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` is a fixed 2-second constant [5](#0-4) [6](#0-5) . The `client_connection_tracker` (and thus the `open_connections` slot) is only dropped/released when `setup_connection` returns, i.e., only after this 2-second timeout elapses for a stalled handshake [7](#0-6) .

There is no address-validation/anti-amplification (Retry) mechanism visible in this code path before the connection is admitted into the `open_connections` count — the endpoints are built with `EndpointConfig::default()` and no explicit retry-token configuration is present in this file [8](#0-7) . The rate limiters that do run beforehand (`overall_connection_rate_limiter`, per-IP `rate_limiter`) are permissive by design — a global token bucket capped at `TOTAL_CONNECTIONS_PER_SECOND = 2500` with `MAX_CONNECTION_BURST = 1000` [9](#0-8)  — which is far larger than `max_concurrent_connections`, so they do not meaningfully throttle a burst designed to fill the concurrent-connection bound within the 2-second window. The code comment itself acknowledges the tradeoff: mitigation is deliberately "shed fast and bound resource consumption" rather than pre-validating IP ownership, specifically to avoid enabling IP-spoofing amplification against third parties [10](#0-9) . This is a conscious design tradeoff, but it does mean that a wave of `max_concurrent_connections` stalled handshakes (whether from real endpoints or via IP rotation) will legitimately occupy every slot for up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, during which `incoming.refuse()` is issued for all other connections [2](#0-1) .

### Impact Explanation
For the duration of `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2 seconds) per wave, all new TPU connection attempts — staked and unstaked alike — are refused via `stats.refused_connections_too_many_open_connections`, since the gating check (`ClientConnectionTracker::new`) happens strictly before any per-peer/stake-based QoS logic (`qos.try_add_connection`) is ever reached. This matches the "Denial of Service — degraded availability of the leader's TPU to legitimate clients" bounty category, since the attacker can repeat the wave every ~2 seconds indefinitely with modest bandwidth (only Initial packets need to be sent, not full handshakes or valid transactions).

### Likelihood Explanation
Feasible for an unstaked, unprivileged remote attacker: it requires only sending valid-looking QUIC Initial packets (no valid TLS cert/stake needed) and declining to complete the handshake (e.g., dropping the client-side Finished flight or not responding to server Handshake packets). Since the concurrent-connection gate applies before rate limiters differentiate staked/unstaked connections or verify peer identity, and the global rate limiter's burst allowance (1000, refills to 2500/s) sits far above typical `max_concurrent_connections` configurations, an attacker can repeatedly refill the bound continuously. This is fully repeatable without needing to control the leader, gossip, or any staked identity.

### Recommendation
Decouple the "in-flight handshake" concurrency bound from the same counter used for QoS admission by:
- Applying a separate, smaller/faster-timing-out bound for handshakes that have not yet been address-validated (e.g., relying on QUIC Retry / anti-amplification validation before admitting into the `open_connections` count), so unverified peers cannot occupy the primary quota.
- Reducing `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` or adding a stricter per-IP concurrent in-flight-handshake cap independent of the global `max_concurrent_connections`, so that a single attacker (or small set of source addresses) cannot consume the entire pool.
- Reserving a portion of `max_concurrent_connections` slots specifically for staked peers, evaluated prior to counting against the shared pool, so unstaked stalling cannot starve staked connections.

### Proof of Concept
Integration test plan (in `streamer/src/nonblocking/quic.rs` test module, or a new integration test):
1. Configure a test QUIC server via `spawn_server` with a small `qos.max_concurrent_connections()` (e.g., 4) for a deterministic test.
2. From `N = max_concurrent_connections` separate client sockets, send a raw QUIC Initial packet (valid header, ALPN `solana-tpu`) to the server's UDP socket and then stop responding (never send subsequent Handshake packets), simulating a stalled attacker handshake.
3. Assert via `stats.open_connections`/`stats.outstanding_incoming_connection_attempts` that all `max_concurrent_connections` slots become occupied.
4. Immediately attempt a legitimate, fully-completing `quinn` client connection (as used in existing tests, e.g., `test_quic_server_*` in `streamer/src/nonblocking/quic.rs` test suite) and assert it is refused (`refused_connections_too_many_open_connections` increments, connection error returned) within a time window less than `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.
5. After waiting past `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`, retry the legitimate client connection and assert it now succeeds, confirming the DoS window matches the timeout duration and repeats indefinitely if the attacker resends stalled Initials in waves.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-76)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;
```

**File:** streamer/src/nonblocking/quic.rs (L80-80)
```rust
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
```

**File:** streamer/src/nonblocking/quic.rs (L161-186)
```rust
            QuicSocket::Kernel(socket) => Endpoint::new(
                EndpointConfig::default(),
                Some(config.clone()),
                socket,
                Arc::new(TokioRuntime),
            )
            .map_err(QuicServerError::EndpointFailed),
            QuicSocket::Xdp(QuicXdpSocketParts {
                socket,
                fallback_src_ip,
                xdp_sender,
            }) => {
                let socket = Arc::new(
                    QuicXdpTxSocket::new(socket, fallback_src_ip, xdp_sender)
                        .map_err(QuicServerError::EndpointFailed)?,
                ) as Arc<dyn AsyncUdpSocket>;
                Endpoint::new_with_abstract_socket(
                    EndpointConfig::default(),
                    Some(config.clone()),
                    socket,
                    Arc::new(TokioRuntime),
                )
                .map_err(QuicServerError::EndpointFailed)
            }
        })
        .collect::<Result<Vec<_>, _>>()?;
```

**File:** streamer/src/nonblocking/quic.rs (L229-234)
```rust
impl Drop for ClientConnectionTracker {
    /// When this is dropped, reduce the open connection count.
    fn drop(&mut self) {
        self.stats.open_connections.fetch_sub(1, Ordering::Relaxed);
    }
}
```

**File:** streamer/src/nonblocking/quic.rs (L236-252)
```rust
impl ClientConnectionTracker {
    /// Check the max_concurrent_connections limit and if it is within the limit
    /// create ClientConnectionTracker and increment open connection count. Otherwise returns Err
    fn new(stats: Arc<StreamerStats>, max_concurrent_connections: usize) -> Result<Self, ()> {
        let open_connections = stats.open_connections.fetch_add(1, Ordering::Relaxed);
        if open_connections >= max_concurrent_connections {
            stats.open_connections.fetch_sub(1, Ordering::Relaxed);
            debug!(
                "There are too many concurrent connections opened already: open: \
                 {open_connections}, max: {max_concurrent_connections}"
            );
            return Err(());
        }

        Ok(Self { stats })
    }
}
```

**File:** streamer/src/nonblocking/quic.rs (L331-340)
```rust
        if let Ok(Some(incoming)) = timeout_connection {
            // our connection/handshake abuse mitigation policy is one of shed
            // fast and bound resource consumption. attempting to be "smarter"
            // before a peer has asserted control over their ip address by
            // completing the retry challenge creates a scenario whereby peers
            // can attack one another via ip spoofing. employ the following
            // * limit duration of in-flight connection attempts with a timeout
            // * protect against connection attempt bursts with a global rate-limiter
            // * rate-limit abusive peers by (control-asserted) ip
            // * cap total connections per-peer/ip
```

**File:** streamer/src/nonblocking/quic.rs (L346-369)
```rust
            // check overall connection request rate limiter
            if overall_connection_rate_limiter.current_tokens() == 0 {
                stats
                    .connection_rate_limited_across_all
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to overall rate limit.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
            // then perform per IpAddr rate limiting
            if !rate_limiter.is_allowed(&incoming.remote_address().ip()) {
                stats
                    .connection_rate_limited_per_ipaddr
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to per-IP rate limiting.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
```

**File:** streamer/src/nonblocking/quic.rs (L371-379)
```rust
            let Ok(client_connection_tracker) =
                ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())
            else {
                stats
                    .refused_connections_too_many_open_connections
                    .fetch_add(1, Ordering::Relaxed);
                incoming.refuse();
                continue;
            };
```

**File:** streamer/src/nonblocking/quic.rs (L384-399)
```rust
            let connecting = incoming.accept();
            match connecting {
                Ok(connecting) => {
                    let rate_limiter = rate_limiter.clone();
                    let overall_connection_rate_limiter = overall_connection_rate_limiter.clone();
                    tasks.spawn(setup_connection(
                        connecting,
                        rate_limiter,
                        overall_connection_rate_limiter,
                        client_connection_tracker,
                        packet_batch_sender.clone(),
                        stats.clone(),
                        quic_server_params.clone(),
                        qos.clone(),
                        tasks.clone(),
                    ));
```

**File:** streamer/src/nonblocking/quic.rs (L471-475)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
```
