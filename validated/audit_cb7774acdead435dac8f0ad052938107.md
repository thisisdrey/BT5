### Title
Unbounded pre-fee TLS signature-verification cost (client can force expensive RSA-PSS/RSA-PKCS1 handshakes before per-IP rate limiting is applied) - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The QUIC server's `crypto_provider()` only restricts `kx_groups` to `X25519`, leaving `signature_verification_algorithms` at the ring default, which includes expensive schemes like RSA-PSS/RSA-PKCS1 in addition to cheap Ed25519. `SkipClientVerification` (used because `client_auth_mandatory()`/`offer_client_auth()` return `true`) always performs full TLS signature verification via `verify_tls13_signature`/`verify_tls12_signature`, and this crypto work happens during the `connecting.await` in `setup_connection`, before `rate_limiter.register_connection()` is invoked.

### Finding Description
`crypto_provider()` in [1](#0-0)  filters `kx_groups` down to `X25519` only but does not touch `signature_verification_algorithms`, so the default ring provider's full set (RSA-PSS, RSA-PKCS1, ECDSA, Ed25519) remains valid for client certificate signatures. `SkipClientVerification::verify_client_cert` accepts any self-signed certificate unconditionally [2](#0-1) , but `verify_tls13_signature`/`verify_tls12_signature` still perform real cryptographic signature verification against the unfiltered algorithm set [3](#0-2) . `client_auth_mandatory()` returns `true` (via `offer_client_auth()`) [4](#0-3) , so every anonymous connection attempt is forced through this signature-verification path during the TLS handshake.

In `setup_connection`, the handshake is awaited via `timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await` [5](#0-4) , and only *after* a successful handshake does the code call `rate_limiter.register_connection(&from.ip())` [6](#0-5) . This confirms the described sequence: crypto-heavy TLS work (including the expensive signature-verification calls) is fully paid for by the server before any per-IP fee/token is charged.

However, this cost is not unbounded system-wide. Before `setup_connection` is even spawned, `run_server`'s accept loop applies: (1) a global `overall_connection_rate_limiter` token-bucket check via `overall_connection_rate_limiter.current_tokens() == 0` [7](#0-6) , (2) a per-IP `rate_limiter.is_allowed()` check (which allows the first connection from any never-before-seen IP but still bounds burst rate for known IPs) [8](#0-7) , and (3) a global `ClientConnectionTracker::new` concurrency cap via `qos.max_concurrent_connections()` that refuses new connections once `open_connections >= max_concurrent_connections` [9](#0-8) . These three gates bound the number of concurrent/in-flight handshakes (and thus the total concurrent signature-verification work) regardless of per-IP registration timing, since `ClientConnectionTracker` is acquired before `connecting.accept()`/`.await` even begins.

### Impact Explanation
The core observation — that expensive signature schemes are not excluded and that per-IP rate-limiter registration happens after handshake completion — is accurate and represents underpriced pre-fee work per single connection compared to Ed25519. However, the *scoped* claim that leader CPU can be exhausted disproportionately hinges on being able to open many parallel handshakes before any throttling applies. The global `overall_connection_rate_limiter` and the global `max_concurrent_connections` cap (`ClientConnectionTracker`) already bound the number of concurrently in-flight (thus concurrently CPU-consuming) handshakes server-wide, independent of per-IP registration timing. This significantly narrows — but does not entirely eliminate — the "disproportionate CPU work per fee" gap, since an attacker can still occupy the bounded pool of concurrent handshake slots with maximally expensive RSA signature verifications instead of cheap Ed25519 ones, increasing per-slot CPU cost within the existing concurrency ceiling.

### Likelihood Explanation
This is plausible under the stated preconditions (`client_auth_mandatory()` always true, unfiltered signature algorithms) but the actual leader-level impact is bounded by the existing concurrency/rate-limiting gates (`overall_connection_rate_limiter`, `ClientConnectionTracker`), which were not accounted for as "already stopping it" in the question's framing. I could not verify the exact numeric values of `MAX_CONNECTION_BURST`, `TOTAL_CONNECTIONS_PER_SECOND`, and `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` within the available tool budget, which are needed to quantify whether the bounded concurrent-handshake slots multiplied by RSA-vs-Ed25519 CPU cost differential exceeds an acceptable budget — this would require a background Devin session with full repo access to confirm precisely.

### Recommendation
Restrict `signature_verification_algorithms` in `crypto_provider()` (tls-utils/src/crypto_provider.rs) to a minimal, cheap set (e.g., Ed25519 only, matching the `kx_groups` filtering approach already used for key exchange), since `SkipClientVerification`/`SkipServerVerification` do not need to support arbitrary certificate types — client identity is only used for pubkey extraction, not PKI trust. This removes the asymmetric CPU cost between signature schemes without weakening the "skip trust verification but still check signature validity" security property.

### Proof of Concept
Because the full quantitative impact assessment (whether existing concurrency caps already bound the disproportionate cost within an acceptable budget) requires reading the exact constant values (`MAX_CONNECTION_BURST`, `TOTAL_CONNECTIONS_PER_SECOND`, `max_concurrent_connections` defaults) and running a CPU benchmark, which I could not complete with the remaining tool budget, I recommend a benchmark-based integration test:
```rust
// streamer/src/nonblocking/quic.rs (test module) or tls-utils integration test
// 1. Spawn a test QUIC server via setup_quic_server() (existing test helper).
// 2. Generate two client certs: one Ed25519, one RSA-4096, both self-signed
//    (see tls-utils quic_client_certificate.rs for cert generation helpers).
// 3. For N in {max_concurrent_connections}, open N parallel client connections
//    using RSA-4096 certs from distinct source ports/IPs (loopback aliases),
//    measure wall/cpu time to complete `setup_connection` up to
//    `rate_limiter.register_connection` being invoked.
// 4. Repeat with Ed25519 certs.
// 5. Assert cpu_time(RSA) / cpu_time(Ed25519) is bounded by a fixed factor
//    (e.g., < 2x) to demonstrate whether crypto_provider()'s unfiltered
//    signature_verification_algorithms creates disproportionate cost within
//    the existing concurrency-capped handshake pool.
```
This test would need to run in an environment with real crypto (not the `RUSTLS_SKIP_...` mocks) and measure actual CPU time via `std::time::Instant` combined with per-thread CPU accounting, to produce the concrete before/after numbers needed to confirm severity.

### Citations

**File:** tls-utils/src/crypto_provider.rs (L3-9)
```rust
pub fn crypto_provider() -> CryptoProvider {
    let mut provider = rustls::crypto::ring::default_provider();
    // Disable all key exchange algorithms except X25519
    provider
        .kx_groups
        .retain(|kx| kx.name() == NamedGroup::X25519);
    provider
```

**File:** tls-utils/src/skip_client_verification.rs (L24-31)
```rust
    fn verify_client_cert(
        &self,
        _end_entity: &CertificateDer,
        _intermediates: &[CertificateDer],
        _now: UnixTime,
    ) -> Result<ClientCertVerified, Error> {
        Ok(ClientCertVerified::assertion())
    }
```

**File:** tls-utils/src/skip_client_verification.rs (L51-63)
```rust
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

**File:** tls-utils/src/skip_client_verification.rs (L69-75)
```rust
    fn offer_client_auth(&self) -> bool {
        true
    }

    fn client_auth_mandatory(&self) -> bool {
        self.offer_client_auth()
    }
```

**File:** streamer/src/nonblocking/quic.rs (L236-251)
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

**File:** streamer/src/nonblocking/quic.rs (L471-477)
```rust
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
        match connecting_result {
```

**File:** streamer/src/nonblocking/quic.rs (L483-493)
```rust
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
