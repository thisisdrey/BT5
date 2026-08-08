### Title
Per-IP and global connection rate limiters can be bypassed by abandoning the QUIC handshake before completion, allowing unbounded pre-fee handshake-crypto CPU consumption from a single IP - (File: streamer/src/nonblocking/quic.rs)

### Summary
`ConnectionRateLimiter::register_connection` and the global `overall_connection_rate_limiter` are only *consumed* after a QUIC handshake completes successfully in `setup_connection`, while the pre-accept checks (`rate_limiter.is_allowed`, `overall_connection_rate_limiter.current_tokens() == 0`) are read-only and never decrement any counters. An attacker who sends Initial packets and forces the server to perform the expensive TLS crypto handshake work but never completes the connection (letting it hit `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`) therefore never triggers `register_connection`, and can repeat this indefinitely from the same source IP without ever being rate limited.

### Finding Description
In `run_server` (`streamer/src/nonblocking/quic.rs`), for each incoming QUIC connection attempt, the server does two *read-only* checks before doing any expensive work:
- `overall_connection_rate_limiter.current_tokens() == 0` [1](#0-0) 
- `rate_limiter.is_allowed(&incoming.remote_address().ip())` [2](#0-1) 

Critically, `ConnectionRateLimiter::is_allowed` only reads `current_tokens` and never consumes a token: `Some(r) => r > 0` / `None => true` [3](#0-2) . The only method that actually decrements the per-IP token bucket is `register_connection`, which calls `consume_tokens` [4](#0-3) .

`register_connection` is invoked exclusively inside `setup_connection`, and only in the branch where the QUIC handshake future (`connecting`) resolved successfully within `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`: [5](#0-4) 

If the handshake instead times out (attacker abandons after triggering server-side crypto but before completing the round trip), the code falls into the `else` branch which merely increments `stats.connection_setup_timeout` and never calls `register_connection` or consumes from `overall_connection_rate_limiter`: [6](#0-5) 

Between the initial `is_allowed` check and the handshake resolution, the server has already: accepted the connection via `incoming.accept()`, allocated a `ClientConnectionTracker` slot (counted against the global `max_concurrent_connections`) [7](#0-6) , and started `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await`, which drives quinn's TLS 1.3 handshake — including asymmetric crypto (ECDHE key agreement and certificate/transcript signing/verification) performed by the server's `rustls`/`quinn` stack using the server's TLS config built via `tls_server_config_builder`/`SkipClientVerification` [8](#0-7) [9](#0-8) . Note that `SkipClientVerification`/`SkipServerVerification` only skip *chain-of-trust* checks, not the actual signature verification math (`verify_tls12_signature`/`verify_tls13_signature` are still invoked) [10](#0-9) [11](#0-10) , so the CPU cost of the crypto handshake is real and comparable to a normal handshake.

Because `is_allowed` is non-mutating, repeating this "connect, force crypto, then go silent" pattern from the same IP never decrements the per-IP token bucket, so `max_connections_per_ipaddr_per_min` never engages for the attacker. The same structural flaw also affects the *global* `overall_connection_rate_limiter` (`TOTAL_CONNECTIONS_PER_SECOND`), since it too is only consumed post-handshake.

The comment in the code acknowledges the general shed-fast design philosophy around spoofing/retry [12](#0-11) , but does not address the asymmetry that a *non-spoofing* attacker (real IP, real return path, capable of receiving the server's handshake response) can force repeated crypto work while remaining invisible to the per-IP token bucket.

The remaining backstops are: the global `max_concurrent_connections` cap (bounds simultaneous outstanding handshakes but is not per-IP, so one attacker can occupy the whole global budget) [13](#0-12) , and the `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` of 2 seconds bounding how long each abandoned attempt can occupy a slot [14](#0-13) . Neither of these is a per-IP throttle on handshake-crypto CPU usage.

### Impact Explanation
An unstaked, unprivileged remote attacker can repeatedly force TPU-facing QUIC servers to perform CPU-bound asymmetric TLS handshake crypto (key exchange + signing/verification) for free, at a rate limited only by the 2-second handshake timeout and the global (not per-IP) `max_concurrent_connections` cap, while `max_connections_per_ipaddr_per_min` is never charged against them. This is a cost-asymmetry / QoS-evasion issue: the invariant that "connection limits enforced per source and cannot be evaded by connection churn" is violated because handshake abandonment specifically evades the only mutation point (`register_connection`) of the per-IP limiter.

### Likelihood Explanation
The attack requires only sending real QUIC Initial/Handshake packets and never completing the handshake — no IP spoofing, no staked/leader control, and no more than standard unstaked client capability is needed. It's trivially scriptable: open N QUIC connections per IP, drive them partway through TLS negotiation, and drop them before `Connecting` resolves, repeating within each 2-second timeout window. This is straightforward and repeatable.

### Recommendation
Consume (or at least partially charge) the per-IP and global rate-limiter tokens at *accept*/pre-handshake time rather than only after successful handshake completion, or introduce a separate per-IP concurrent in-flight-handshake counter that is charged before `connecting.accept()` and released on both success and timeout/failure. This ensures repeated abandoned handshake attempts from the same IP are still throttled.

### Proof of Concept
Rust integration test plan (extends existing `streamer/src/nonblocking/quic.rs` test harness, e.g. `setup_quic_server`):
1. Configure server with `max_connections_per_ipaddr_per_min: 1` (as in `test_rate_limiting_establish_connection` [15](#0-14) ).
2. From a single client IP, repeatedly create a `quinn::Endpoint::connect` and, instead of `.await`ing to completion, drop the `Connecting` future (or delay ACKing the server's handshake response) immediately after the first flight is sent, so the connection never resolves and the server hits `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`.
3. Repeat this N times (N > per-minute limit) within a short window.
4. Assert `stats.connection_setup_timeout` increments N times while `stats.connection_rate_limited_per_ipaddr` stays at 0 (proving `register_connection`/rate limiting was never triggered) — demonstrating the per-IP limiter never engages despite repeated handshake-crypto work.
5. Then perform one real, completed connection from the same IP and show it succeeds (limiter never decremented despite N prior attempts), confirming the bypass.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L78-80)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L332-340)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L346-357)
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
```

**File:** streamer/src/nonblocking/quic.rs (L358-369)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L472-493)
```rust
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

**File:** streamer/src/nonblocking/quic.rs (L538-543)
```rust
    } else {
        stats
            .connection_setup_timeout
            .fetch_add(1, Ordering::Relaxed);
    }
}
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L34-40)
```rust
    pub fn is_allowed(&self, ip: &IpAddr) -> bool {
        // Check if we have records in the rate limiter for the given IP address
        match self.limiter.current_tokens(ip) {
            Some(r) => r > 0, // we have a record, and rate is not exceeded
            None => true,     // if we have not seen IP, allow connection request
        }
    }
```

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L42-50)
```rust
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

**File:** streamer/src/quic.rs (L100-104)
```rust
    let mut server_tls_config =
        tls_server_config_builder().with_single_cert(vec![cert], priv_key)?;
    server_tls_config.alpn_protocols = vec![ALPN_TPU_PROTOCOL_ID.to_vec()];
    server_tls_config.key_log = Arc::new(KeyLogFile::new());
    let quic_server_config = QuicServerConfig::try_from(server_tls_config)?;
```

**File:** tls-utils/src/config.rs (L16-20)
```rust
pub fn tls_server_config_builder() -> ConfigBuilder<ServerConfig, WantsServerCert> {
    ServerConfig::builder_with_provider(Arc::new(crate::crypto_provider()))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_client_cert_verifier(crate::SkipClientVerification::new())
```

**File:** tls-utils/src/skip_client_verification.rs (L37-63)
```rust
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }
```

**File:** tls-utils/src/skip_server_verification.rs (L27-54)
```rust
impl ServerCertVerifier for SkipServerVerification {
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls12_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, Error> {
        verify_tls13_signature(
            message,
            cert,
            dss,
            &self.0.signature_verification_algorithms,
        )
    }
```

**File:** tpu-client-next/tests/connection_workers_scheduler_test.rs (L670-687)
```rust
async fn test_rate_limiting_establish_connection() {
    let SpawnTestServerResult {
        join_handle: server_handle,
        receiver,
        server_address,
        stats: _stats,
        cancel,
    } = setup_quic_server(
        None,
        QuicStreamerConfig {
            max_connections_per_ipaddr_per_min: 1,
            ..QuicStreamerConfig::default_for_tests()
        },
        SwQosConfig {
            max_connections_per_unstaked_peer: 100,
            ..Default::default()
        },
    );
```
