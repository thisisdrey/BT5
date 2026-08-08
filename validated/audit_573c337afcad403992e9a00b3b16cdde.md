### Title
Per-IP QUIC connection rate limit bypass via check-then-register race - ([File: streamer/src/nonblocking/connection_rate_limiter.rs])

### Summary
`ConnectionRateLimiter::is_allowed` is a non-mutating read of the token bucket state, while `ConnectionRateLimiter::register_connection` is the only call that actually consumes a token. Because these are separate calls invoked before and after the QUIC handshake (`connecting.await`) in `run_server`, an attacker can open many concurrent handshakes from a single IP that all pass the `is_allowed` pre-check before any of them call `register_connection`, allowing the effective per-IP connection burst to exceed `max_connections_per_ipaddr_per_min`.

### Finding Description
`is_allowed` only reads the current token count via `self.limiter.current_tokens(ip)` and returns `true` if tokens remain (or if the IP has no record yet); it performs no reservation or decrement, as explicitly documented and tested in the module's own unit test ("just checking should not mutate state") [1](#0-0) . Token consumption only happens later in `register_connection`, which calls `self.limiter.consume_tokens(*ip, 1)` [2](#0-1) .

Per the question's stated call flow in `run_server`, `is_allowed` is invoked before `connecting.await` (the async QUIC handshake, which can take up to `QUIC_CONNECTION_HANDSHAKE_TIMEOUT`), and `register_connection` is only invoked after the handshake completes. Since `is_allowed` never marks tokens as "reserved" or "in-flight," any number of concurrent handshakes from the same IP that are initiated within the handshake timeout window will each independently observe `is_allowed(ip) == true` (because none of them have yet reached `register_connection` to decrement the bucket), and only after they all complete will `register_connection` be called, at which point the bucket is decremented multiple times but the accept/CPU work has already occurred.

This is a classic time-of-check-to-time-of-use (TOCTOU) gap: the check (`is_allowed`) and the mutation (`register_connection`) are not atomic with respect to each other, and nothing in `ConnectionRateLimiter` reserves a slot at check time. An attacker who fires N `Initial` QUIC packets in parallel from one IP can force N handshakes to proceed concurrently, each seeing the same pre-consumption token count.

### Impact Explanation
This allows an unstaked, single-IP attacker to transiently exceed the configured `max_connections_per_ipaddr_per_min`, consuming disproportionate QUIC handshake CPU and `ClientConnectionTracker`/connection-table slots for zero fee, which is the "grossly underpriced pre-fee work" / QoS-evasion category referenced in the scope. The impact is bounded by how many parallel handshakes the attacker can complete within roughly the `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` window per burst, not an unbounded amplification, since the rate limiter still eventually catches up once `register_connection` is called on completed handshakes.

### Likelihood Explanation
Feasible and repeatable: this requires only network reachability to the TPU QUIC port from a single IP address, no stake, and standard QUIC client libraries capable of issuing concurrent connection attempts (e.g., multiple endpoints/connections in parallel from the same source address). The race window is bounded by the handshake timeout, so the attacker can repeat this burst pattern periodically to sustain elevated pressure on the per-IP limiter's ceiling.

### Recommendation
Make the check-and-reserve step atomic: instead of calling `is_allowed` (read-only) before the handshake and `register_connection` (consuming) after, reserve a token at accept time before starting the handshake (e.g., call a single atomic "try_consume" style method immediately after accepting the raw connection, before `connecting.await`), and release/refund the token if the handshake fails or times out. This removes the check-then-act gap by ensuring token consumption happens at the earliest point in the connection lifecycle, not after the expensive handshake work has already occurred.

### Proof of Concept
Integration test plan (extends the existing `test_connection_rate_limiter` pattern with concurrency):
```rust
// streamer/src/nonblocking/quic.rs (test module) or a new integration test
#[tokio::test]
async fn test_concurrent_handshake_bypasses_rate_limit() {
    // Setup a QUIC server via setup_quic_server with
    // max_connections_per_ipaddr_per_min = 1 (only 1 connection/min allowed per IP).
    // Spawn N (e.g., 10) client QUIC endpoints from the same source IP,
    // all calling connect() concurrently (e.g., via tokio::join! / FuturesUnordered)
    // within QUIC_CONNECTION_HANDSHAKE_TIMEOUT.
    //
    // Expected (buggy) behavior: StreamerStats::total_new_connections
    // ends up > 1 (i.e., > configured limit), because is_allowed() was
    // read by all N tasks before any of them called register_connection().
    //
    // Assertion that should hold if fixed:
    // assert!(stats.total_new_connections.load(Ordering::Relaxed) <= 1);
    // Currently this assertion is expected to fail, demonstrating the race.
}
```
This can be directly derived from the existing single-threaded unit test in `connection_rate_limiter.rs` [3](#0-2)  by replacing sequential `is_allowed`/`register_connection` calls with concurrent tasks that call `is_allowed` first and `register_connection` only after an artificial delay (simulating handshake time), showing that all N tasks observe `is_allowed == true`.

### Citations

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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L57-81)
```rust
    #[tokio::test]
    async fn test_connection_rate_limiter() {
        let limiter = ConnectionRateLimiter::new(3, 3, 4);
        let ip1 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(limiter.is_allowed(&ip1));
        assert!(limiter.register_connection(&ip1));
        assert!(!limiter.is_allowed(&ip1));
        assert!(!limiter.register_connection(&ip1));

        let ip2 = IpAddr::V4(Ipv4Addr::new(192, 168, 1, 2));
        for _ in 0..100 {
            assert!(
                limiter.is_allowed(&ip2),
                "just checking should not mutate state"
            );
        }
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(limiter.is_allowed(&ip2));
        assert!(limiter.register_connection(&ip2));
        assert!(!limiter.is_allowed(&ip2));
    }
```
