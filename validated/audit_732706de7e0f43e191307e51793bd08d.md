This confirms the finding: the signer maintainers explicitly document that `endpoint` is expected to be a "local or subnet network stacks node" connection, but the code itself contains no authentication check whatsoever on the `/shutdown` path (`libsigner/src/events.rs:443-445`), and the warning at `stacks-signer/src/lib.rs:125-132` is only a doc-string, not an enforced control — the socket is bound per `config.endpoint` via `SignerEventReceiver::bind` at `libsigner/src/events.rs:404-408` and there is no code path that restricts binding to loopback or that checks any secret/signature on incoming HTTP requests. Given the specified attacker precondition ("directly connecting to the signer's bound listener address"), the `/shutdown` handler unconditionally sets `stop_signal` and returns `EventError::Terminated`, which `main_loop` (`libsigner/src/events.rs:296-300`) treats identically to a legitimate `get_stop_signaler().send()` call.

### Title
Unauthenticated `POST /shutdown` permanently halts the signer's event-processing loop - (File: libsigner/src/events.rs)

### Summary
The signer's HTTP event-listener treats any `POST /shutdown` request as equivalent to the trusted internal `SignerStopSignaler::send()` call, setting the shared `stop_signal` atomic with no signature, secret, or peer-identity check. Anyone who can open a TCP connection to the signer's bound event-receiver address can permanently stop the signer's `main_loop`, taking it offline for all future block-response/consensus duties until manually restarted.

### Finding Description
`SignerEventReceiver::next_event` dispatches purely on `request.url()`: when the URL is `/shutdown`, it stores `true` into `stop_signal` and returns `Err(EventError::Terminated)` with no check of any credential [1](#0-0) . This is the exact same `stop_signal: Arc<AtomicBool>` written by the legitimate, in-process `SignerStopSignaler::send()` [2](#0-1) . `main_loop` treats `EventError::Terminated` as a clean, intentional shutdown and `break`s out of the loop unconditionally [3](#0-2) . Once stopped, `is_stopped()` is `true` forever (no reset path exists), so the receiver never processes another event and the thread running `main_loop` exits for good [4](#0-3) . The only "check" performed is a URL-string match; there is no signature, no shared secret, and no verification that the request originated from the local node process. The `bind()` implementation opens the socket at whatever `SocketAddr` is passed via configuration with no restriction to loopback [5](#0-4) , and `SpawnedSigner::new` only emits a warning log recommending operators use a local network — it enforces nothing in code [6](#0-5) . An attacker who can reach that address sends one crafted HTTP request: `POST /shutdown HTTP/1.1` with any body, and the signer's event thread halts, requiring no admin role, RPC secret, or slot ownership.

### Impact Explanation
A single unauthenticated message permanently stops the signer's event-processing thread, which is responsible for consuming StackerDB chunks, block proposals, and burn-block events used for the signer's consensus-relevant block-signing duties. Since `is_stopped()` never resets, the signer is fully deaf to further events until the operator manually restarts the process — this matches the "Critical: unauthenticated DoS from few messages" category, repeatable indefinitely (one message per restart cycle) against any specific reachable signer.

### Likelihood Explanation
The only precondition is TCP reachability to the signer's configured event-receiver `endpoint` address/port — no secret, signature, peer identity, or StackerDB slot is required. Attacker cost is a single raw HTTP POST. While operator guidance recommends binding this endpoint to a local/subnet interface, this is a deployment-time recommendation only, not an enforced control in the code, so any deployment that binds the endpoint to a non-loopback interface (e.g., a subnet or misconfigured `0.0.0.0`) is fully exposed to any peer that can route to it.

### Recommendation
Add authentication to the `/shutdown` path (and ideally to all POST endpoints in `next_event`): require a pre-shared secret/HMAC known only to the local node and signer, or bind the stop-signal channel to a Unix domain socket / dedicated loopback-only listener separate from the externally-facing event ports, so that the `/shutdown` request can only originate from `SignerStopSignaler::send()` itself rather than from arbitrary remote clients matching a URL string.

### Proof of Concept
In a new test module in `libsigner/src/events.rs` (or `libsigner/src/tests/mod.rs`):
1. Construct a `SignerEventReceiver::<SomeEventType>::new(false)` and call `bind()` on `127.0.0.1:0` to get an ephemeral port; record the returned `SocketAddr`.
2. Spawn `main_loop()` on a background thread.
3. From the test thread, open a raw `TcpStream::connect(addr)` and write:
   `"POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: 4\r\nContent-Type: text/plain\r\n\r\ntest"`
   — note this is built directly by the test, not via `get_stop_signaler()`.
4. Assert that within a short timeout `receiver.is_stopped()` becomes `true` and the spawned thread's `JoinHandle` completes (`is_finished()`/`join()` returns), demonstrating termination occurred purely from the crafted socket write.
5. Optionally, repeat the connect+POST from a second raw socket after restarting the receiver to show the attack is trivially repeatable per restart cycle, with no `get_stop_signaler()` instance ever constructed in the test.

### Citations

**File:** libsigner/src/events.rs (L284-312)
```rust
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
    }
```

**File:** libsigner/src/events.rs (L378-382)
```rust
    fn send(&mut self) {
        self.stop_signal.store(true, Ordering::SeqCst);
        // wake up the thread so the atomicbool can be checked
        // This makes me sad...but for now...it works.
        if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** stacks-signer/src/lib.rs (L122-132)
```rust
        let endpoint = config.endpoint;
        info!("Stacks signer version {:?}", VERSION_STRING.as_str());
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
