### Title
Single unprivileged IP can burst-drain the global QUIC connection-accept token bucket via non-atomic peek checks before per-IP rejection - (File: streamer/src/nonblocking/quic.rs)

### Summary
The connection accept loop in `run_server` uses non-mutating "peek" checks (`overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed`) before actually consuming tokens, and the mutating per-IP check (`register_connection`) is deferred to the async `setup_connection` task that runs after the costly QUIC handshake accept. Because many connection attempts from the same source IP can be dispatched concurrently and all pass the non-mutating peek checks before any of them registers, a single IP can consume a disproportionate share of the shared, global `overall_connection_rate_limiter` budget (capped at `MAX_CONNECTION_BURST = 1000`, refilled at `TOTAL_CONNECTIONS_PER_SECOND = 2500`) prior to being individually rejected by the per-IP token bucket.

### Finding Description
In `run_server` [1](#0-0) , the incoming-connection loop performs:
1. A peek-only check `overall_connection_rate_limiter.current_tokens() == 0` — this reads the bucket but does not consume a token.
2. A peek-only check `rate_limiter.is_allowed(&ip)` — per `ConnectionRateLimiter::is_allowed`, this only calls `current_tokens` and explicitly does **not** mutate state [2](#0-1) .

Only after these non-mutating peeks does the loop call `incoming.accept()` (performing the actual QUIC handshake) and spawn `setup_connection`, where the real, atomic, mutating consumption — `rate_limiter.register_connection` (which calls `self.limiter.consume_tokens`) — takes place [3](#0-2) , along with consumption of `overall_connection_rate_limiter` cloned into the spawned task [4](#0-3) .

Because `run_server`'s main loop dispatches each accepted `Incoming` to its own spawned task (`tasks.spawn(setup_connection(...))`), many connection attempts originating from the same attacker IP within the same tick can each individually pass the cheap, non-mutating `overall_connection_rate_limiter.current_tokens() == 0` and `rate_limiter.is_allowed` peek checks *before* any of the concurrently-running `setup_connection` tasks has consumed a token via `register_connection`/`consume_tokens`. This is a classic check-then-act (TOCTOU) race: the gating decision (peek) is decoupled in time and concurrency from the mutation (consume), across an intervening expensive operation (`incoming.accept()`, i.e., completing the QUIC handshake).

The comment in `ConnectionRateLimiter::is_allowed` even documents the design intent — "we should only modify server state once source IP is verified" — confirming that state mutation is deliberately deferred past the handshake, which is exactly the window an attacker can exploit with connection churn/bursting from one IP to grab a disproportionate share of the shared, global `overall_connection_rate_limiter` bucket before the per-IP limiter can catch up and reject the excess.

### Impact Explanation
This falls under the described QoS/rate-limit-evasion bounty category: an unprivileged, unstaked single-IP attacker can unfairly capture a disproportionate share of the shared, global connection-accept budget (`MAX_CONNECTION_BURST` / `TOTAL_CONNECTIONS_PER_SECOND`), starving other legitimate unstaked senders of the ability to even open a TPU QUIC connection during the burst window, before the per-IP limiter's mutating `register_connection` check eventually rejects the excess attempts. The cost asymmetry — attacker pays only its own accept-side handshake cost, while the shared bucket used by all other unstaked clients gets drained by a single IP's burst — is the essence of the unfair-capture impact scoped for this file.

### Likelihood Explanation
Preconditions are default config (`max_connections_per_ipaddr_per_min`, default `MAX_CONNECTION_BURST = 1000`, `TOTAL_CONNECTIONS_PER_SECOND = 2500.0`), no staking or special privilege required, and no IP spoofing needed — the attacker simply opens many concurrent real QUIC connections from one machine/IP. The exploit only requires enough concurrency to win the race between the peek checks and the deferred mutation, which is readily achievable with standard async client tooling. Repeatability is high since the race window reopens every time a burst of concurrent attempts is sent.

### Recommendation
Make the global and per-IP admission checks atomic with respect to token consumption instead of separating "peek" and "consume": call `overall_connection_rate_limiter.consume_tokens` (not just `current_tokens`) and `rate_limiter.register_connection` (not `is_allowed`) synchronously in the main accept loop *before* calling `incoming.accept()`/spawning `setup_connection`, so that token consumption gating happens prior to the costly handshake and prior to any concurrency window that lets multiple attempts from one IP race past the check.

### Proof of Concept
Integration test plan (async, using `tokio`):
1. Instantiate `ConnectionRateLimiter::new(limit_per_minute, max_burst, num_shards)` and a shared `TokenBucket` mirroring `overall_connection_rate_limiter` configuration.
2. Spawn N concurrent tasks (N > max_burst for the single attacker IP) that call `rate_limiter.is_allowed(&attacker_ip)` and, in parallel, `overall_connection_rate_limiter.current_tokens()`/`consume_tokens` — modeling the exact non-atomic sequence in `run_server` (peek both, then simulate the accept latency with a short `tokio::time::sleep`, then call `register_connection`/`consume_tokens`).
3. Assert: the number of tasks that pass the initial peek checks and proceed to "accept" (consume global tokens) exceeds what a strictly-serialized check-then-consume design would allow, i.e., more than `max_burst` per-IP tokens or more than the expected fair share of `MAX_CONNECTION_BURST` are consumed from the shared bucket by the single attacker IP during the race window, while a second control IP sending far fewer but properly-serialized requests is left with insufficient global tokens.
4. Expected assertion under the current implementation: `overall_bucket.current_tokens()` after the burst is measurably lower than `MAX_CONNECTION_BURST - (control_ip_requests)`, demonstrating disproportionate consumption attributable to the attacker IP's concurrent burst rather than the fair per-IP allotment.

### Citations

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

**File:** streamer/src/nonblocking/connection_rate_limiter.rs (L31-40)
```rust
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
