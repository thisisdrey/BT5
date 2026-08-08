## Title
Single-IP QUIC handshake-slot exhaustion via `ClientConnectionTracker` before per-IP `register_connection` gating — (File: `streamer/src/nonblocking/quic.rs`)

## Summary
`ConnectionRateLimiter::register_connection` is only invoked *after* a QUIC handshake completes, in `setup_connection`, while the pre-handshake gate in `run_server` (`rate_limiter.is_allowed`) is a read-only check on already-consumed tokens and does not itself consume tokens or block a burst of new, never-before-seen source IPs. This lets a single unstaked attacker occupy a large share of the global `max_concurrent_connections` slots (tracked via `ClientConnectionTracker`) for the full `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window by sending Initial packets and never completing the handshake.

## Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs:304-410`), for each incoming connection the server does, in order:
1. Global rate check: `overall_connection_rate_limiter.current_tokens() == 0` [1](#0-0) 
2. Per-IP check: `rate_limiter.is_allowed(&incoming.remote_address().ip())` [2](#0-1) 
3. `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())`, which increments the global `open_connections` counter and fails only once that global cap is reached [3](#0-2) 
4. The connection is then handed to `setup_connection`, which waits up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) for the handshake to complete [4](#0-3) [5](#0-4) 

Crucially, `ConnectionRateLimiter::is_allowed` only inspects the *existing* token count for that IP; it returns `true` for any IP it has not seen before (`None => true`) and does not itself consume/register a token [6](#0-5) . Tokens are only consumed by `register_connection`, and that call happens exclusively inside `setup_connection` *after* the handshake successfully completes (`Ok(new_connection) => { ... rate_limiter.register_connection(&from.ip()) ... }`) [7](#0-6) .

Consequently, an attacker from one source IP can send a burst of QUIC Initial packets (ClientHello) and simply never respond to the server's handshake response. Each such attempt:
- passes the per-IP `is_allowed` check every time (no tokens consumed yet for that IP),
- is only bounded by the *global* `overall_connection_rate_limiter` (burst `MAX_CONNECTION_BURST = 1000`, sustained `TOTAL_CONNECTIONS_PER_SECOND = 2500`) [8](#0-7) [9](#0-8) ,
- consumes a `ClientConnectionTracker` slot against the process-wide `max_concurrent_connections` cap, computed as `(max_staked_connections + max_unstaked_connections) * 5 / 4` in `SwQos::max_concurrent_connections` [10](#0-9) ,
- and holds that slot for the full 2-second handshake timeout (the timer starts on Initial receipt and is only canceled by a Handshake packet, per the code comment) [4](#0-3) , before `stats.outstanding_incoming_connection_attempts.fetch_sub` and tracker drop free it up.

Because `max_concurrent_connections` is a single global (non-per-IP) atomic counter (`ClientConnectionTracker::new`/`Drop` on `stats.open_connections`), a single IP that keeps this pipeline full every ~2 seconds can push `open_connections` up to the cap and cause subsequent `ClientConnectionTracker::new` calls (for legitimate unstaked senders arriving at other IPs) to fail, incrementing `refused_connections_too_many_open_connections` and dropping their `Incoming` (`incoming.refuse()`) [11](#0-10) . No per-IP handshake-attempt cap exists to stop this — the only IP-scoped defense (`ConnectionRateLimiter`) is gated on handshake completion, which the attacker deliberately avoids.

## Impact Explanation
This is a real, reachable denial-of-service against TPU QUIC ingress for legitimate unstaked (and potentially staked, since the cap is on total open connections/handshakes in flight, shared with `qos.max_concurrent_connections()`) senders, achievable by a single unprivileged, unstaked remote attacker without completing any handshake and without holding stake. It matches the "QoS evasion / resource exhaustion via connection churn" category in scope, since the per-source-IP connection-rate control (`ConnectionRateLimiter`) is bypassed entirely during the pre-handshake phase, defeating the intended invariant that per-IP limits bound resource consumption per source.

## Likelihood Explanation
Preconditions are minimal: the attacker only needs to open raw UDP sockets and send QUIC Initial packets to the leader's public TPU QUIC port, then withhold further handshake packets. This requires no stake, no valid keypair, and can be repeated indefinitely (each cycle bounded only by the 2-second handshake timeout and the global token bucket's replenishment, which refills at up to 2500 tokens/sec — far more than needed to refill the attacker's own churn). The attack is fully repeatable and does not require multiple IPs, though spreading across a modest number of source IPs trivially defeats even the eventual per-IP `register_connection` penalty (since penalties only apply after a completed handshake, which never occurs).

## Recommendation
Introduce a pre-handshake, per-IP admission control that is independent of `register_connection`/handshake completion — e.g., track and cap the number of `outstanding_incoming_connection_attempts` per source IP (not just a global counter), or consume a token from `ConnectionRateLimiter` for the attempt itself (before/at `ClientConnectionTracker::new`) and refund it if the handshake later completes successfully within budget, rather than only charging on success. Alternatively, reserve only a bounded fraction of `max_concurrent_connections` for pending (pre-handshake) connections per IP, distinct from the pool used by fully-established connections, so that a single IP cannot monopolize the global handshake-in-progress slots.

## Proof of Concept
Rust integration test plan (extending the existing `streamer/src/nonblocking/quic.rs` test module, using `setup_quic_server`):
1. Configure a `QuicStreamerConfig`/`SwQosConfig` with a small `max_concurrent_connections` (e.g., derived from `max_staked_connections=0, max_unstaked_connections=4` → cap of 5).
2. From a single source IP, open `N > max_concurrent_connections` raw QUIC `Connecting` attempts (e.g., via `quinn::Endpoint::connect`) but do not drive them to completion (drop the future/endpoint immediately after sending the Initial, or use a client endpoint configured to stall the handshake, e.g. by never responding to server's Handshake flight — this can be simulated by connecting to a black-holed/duplicate socket or by pausing after `connecting` future starts).
3. Assert that `stats.outstanding_incoming_connection_attempts` climbs up to (but is bounded by) `max_concurrent_connections`, and that once the cap is hit, `stats.refused_connections_too_many_open_connections` increments for further attempts — even though all attempts originate from the same IP and `stats.connection_rate_limited_per_ipaddr` stays at 0 (proving the per-IP limiter never engaged).
4. Additionally attempt one legitimate `make_client_endpoint` connection from a second IP during the window and assert it is refused (`incoming.refuse()` observed as connection error) due to `refused_connections_too_many_open_connections`, demonstrating denial of service to an uninvolved sender. [12](#0-11) [13](#0-12) [14](#0-13)

### Citations

**File:** streamer/src/nonblocking/quic.rs (L70-80)
```rust
/// Total new connection counts per second. Heuristically taken from
/// the default staked and unstaked connection limits. Might be adjusted
/// later.
const TOTAL_CONNECTIONS_PER_SECOND: f64 = 2500.0;

/// Max burst of connections above sustained rate to pass through
const MAX_CONNECTION_BURST: u64 = 1000;

/// Timeout for connection handshake. Timer starts once we get Initial from the
/// peer, and is canceled when we get a Handshake packet from them.
const QUIC_CONNECTION_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);
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

**File:** streamer/src/nonblocking/quic.rs (L277-281)
```rust
    let overall_connection_rate_limiter = Arc::new(TokenBucket::new(
        MAX_CONNECTION_BURST,
        MAX_CONNECTION_BURST,
        TOTAL_CONNECTIONS_PER_SECOND,
    ));
```

**File:** streamer/src/nonblocking/quic.rs (L304-410)
```rust
    loop {
        let timeout_connection = select! {
            ready = accepts.next() => {
                if let Some((connecting, i)) = ready {
                    accepts.push(
                        Box::pin(EndpointAccept {
                            accept: endpoints[i].accept(),
                            endpoint: i,
                        }
                    ));
                    Ok(connecting)
                } else {
                    // we can't really get here - we never poll an empty FuturesUnordered
                    continue
                }
            }
            _ = tokio::time::sleep(WAIT_FOR_CONNECTION_TIMEOUT) => {
                Err(())
            }
            _ = cancel.cancelled() => break,
        };

        if last_datapoint.elapsed().as_secs() >= 5 {
            stats.report(name);
            last_datapoint = Instant::now();
        }

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

            stats
                .total_incoming_connection_attempts
                .fetch_add(1, Ordering::Relaxed);

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

            let Ok(client_connection_tracker) =
                ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())
            else {
                stats
                    .refused_connections_too_many_open_connections
                    .fetch_add(1, Ordering::Relaxed);
                incoming.refuse();
                continue;
            };

            stats
                .outstanding_incoming_connection_attempts
                .fetch_add(1, Ordering::Relaxed);
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
                }
                Err(err) => {
                    stats
                        .outstanding_incoming_connection_attempts
                        .fetch_sub(1, Ordering::Relaxed);
                    debug!("Incoming::accept(): error {err:?}");
                }
            }
        } else {
            debug!("accept(): Timed out waiting for connection");
        }
```

**File:** streamer/src/nonblocking/quic.rs (L456-493)
```rust
#[allow(clippy::too_many_arguments)]
async fn setup_connection<Q, C>(
    connecting: Connecting,
    rate_limiter: Arc<ConnectionRateLimiter>,
    overall_connection_rate_limiter: Arc<TokenBucket>,
    client_connection_tracker: ClientConnectionTracker,
    packet_sender: Sender<PacketBatch>,
    stats: Arc<StreamerStats>,
    server_params: Arc<QuicStreamerConfig>,
    qos: Arc<Q>,
    tasks: TaskTracker,
) where
    Q: QosController<C> + Send + Sync + 'static,
    C: ConnectionContext + Send + Sync + 'static,
{
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
        match connecting_result {
            Ok(new_connection) => {
                debug!("Got a connection {from:?}");
                // now that we have observed the handshake we can be certain
                // that the initiator owns an IP address, we can update rate
                // limiters on the server
                if !rate_limiter.register_connection(&from.ip()) {
                    debug!("Reject connection from {from:?} -- rate limiting exceeded");
                    stats
                        .connection_rate_limited_per_ipaddr
                        .fetch_add(1, Ordering::Relaxed);
                    new_connection.close(
                        CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                        CONNECTION_CLOSE_REASON_DISALLOWED,
                    );
                    return;
                }
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L21-50)
```rust
    pub fn new(limit_per_minute: u64, max_burst: u64, num_shards: usize) -> Self {
        Self {
            limiter: KeyedRateLimiter::new(
                CONNECTION_RATE_LIMITER_CLEANUP_SIZE_THRESHOLD,
                TokenBucket::new(limit_per_minute, max_burst, limit_per_minute as f64 / 60.0),
                num_shards,
            ),
        }
    }

    /// Check if the connection from the said `ip` is allowed.
    /// Here we assume that only IPs with actual confirmed connections are stored in it,
    /// since we should only modify server state once source IP is verified
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }

    pub fn register_connection(&self, ip: &IpAddr) -> bool {
        if self.limiter.consume_tokens(*ip, 1).is_ok() {
            debug!("Request from IP {ip:?} allowed");
            true // Request allowed
        } else {
            debug!("Request from IP {ip:?} blocked");
            false // Request blocked
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```
