This confirms the vulnerability is real and precisely matches the question's claim.

### Title
Remote unauthenticated thread panic on `/metrics` via early client disconnect - (File: stacks-signer/src/monitoring/server.rs)

### Summary
`MonitoringServer::main_loop` calls `request.respond(response).expect("Failed to send response")` when serving `GET /metrics`. If the remote client closes/resets the TCP connection before the response body is fully written, `tiny_http`'s `respond()` returns an `Err(io::Error)` (broken pipe/connection reset), and the `.expect()` unwraps it into a panic, killing the `signer_metrics` thread that runs `main_loop` for good.

### Finding Description
The broken equality is: "a remote peer closing its socket mid-response" is assumed to equal "the server thread survives to serve the next request," but the code does not hold this invariant. In `main_loop` at [1](#0-0) , the handler for `/metrics` unconditionally calls `.expect()` on the result of `request.respond(...)`. `tiny_http::Request::respond` performs actual socket writes and can fail with an I/O error (e.g., `ECONNRESET`/`EPIPE`) if the peer closed the connection before the response was fully transmitted. Since this handler has no error handling — unlike a hypothetical `if let Err(e) = ... { log }` pattern — any write failure becomes an unrecoverable panic.

The server is started once via `start_serving_monitoring_metrics` in [2](#0-1) , spawning a single dedicated OS thread named `signer_metrics` running `MonitoringServer::start` → `main_loop`. There is no supervisor/restart logic; once this thread panics, the monitoring/metrics endpoint is permanently dead for the life of the signer process (no automatic re-spawn), since `spawn`'s `JoinHandle` is discarded (`let _ = ...`) and never joined/monitored.

No auth gate, length cap, or signature check exists on this HTTP path since `/metrics` is a plaintext, unauthenticated local-monitoring endpoint by design — the only remaining protection against process-level failures would be to handle the I/O error from `respond()`, but that isn't done. Note the same `.expect()` pattern also exists on the other endpoints (`/info`, `/`, `/heartbeat`, 404) at lines 141, 149, 162, 169, so the flaw is systemic across `main_loop`, not just `/metrics`.

### Impact Explanation
A single unauthenticated TCP connection making a valid `GET /metrics` request, then closing early (RST) before the response is fully written, panics the `signer_metrics` thread and permanently ends the metrics HTTP server for the process's lifetime (no crash-loop respawn). This matches the "Critical: remote crash/unauthenticated DoS from few messages" category — it disables liveness/alerting monitoring for the signer with a single crafted disconnect, and is trivially repeatable against any signer configured with `metrics_endpoint`.

### Likelihood Explanation
Preconditions: the operator must have configured `metrics_endpoint` (common in production signer deployments for monitoring/alerting) — see the guard in `start_serving_monitoring_metrics`. No signer key, StackerDB slot, or privileged role is needed; only network reachability to the metrics port. Attacker cost is a single TCP connection and a partial-write race (trivial to force reliably via `shutdown(Both)`/RST immediately after sending the request), and the attack is fully repeatable at will.

### Recommendation
Replace all `.expect(...)` calls on `request.respond(...)` in `main_loop` (lines 134, 141, 149, 162, 169) with proper error handling, e.g., `if let Err(e) = request.respond(response) { warn!("Monitoring: failed to send response: {:?}", e); }`, so a client disconnect only logs a warning instead of panicking the thread. Additionally, consider wrapping the metrics server thread in a supervisor loop that restarts `MonitoringServer::start` on unexpected termination.

### Proof of Concept
Rust test (in `stacks-signer/src/monitoring` test module, `monitoring_prom` feature):
1. Bind a `tiny_http::Server` to `127.0.0.1:0`, construct a `MonitoringServer`, and spawn `main_loop()` on a named thread (mirroring `signer_metrics`).
2. From the test's main thread, open a raw `TcpStream` to the bound address and write a minimal valid HTTP request: `b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n"`.
3. Immediately call `stream.shutdown(std::net::Shutdown::Both)` (or drop the stream without reading the response) to force the server's write to fail with a broken-pipe/connection-reset error.
4. Join the spawned thread and assert `handle.join().is_err()` (thread panicked), confirming the panic occurs at `request.respond(response).expect("Failed to send response")` (`stacks-signer/src/monitoring/server.rs:134`).

### Citations

**File:** stacks-signer/src/monitoring/server.rs (L132-136)
```rust
            if request.url() == "/metrics" {
                let response = HttpResponse::from_string(gather_metrics_string());
                request.respond(response).expect("Failed to send response");
                continue;
            }
```

**File:** stacks-signer/src/monitoring/mod.rs (L178-184)
```rust
        let _ = std::thread::Builder::new()
            .name("signer_metrics".to_string())
            .spawn(move || {
                if let Err(monitoring_err) = super::server::MonitoringServer::start(&config) {
                    error!("Monitoring: Error in metrics server: {:?}", monitoring_err);
                }
            });
```
