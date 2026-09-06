I have enough confirmation. There's no authentication check on the `/shutdown` path — the same HTTP server that accepts `/stackerdb_chunks` from the node also accepts `/shutdown` from anyone who can reach the listener, with only a URL-string match and no secret/token/origin verification.### Title
Unauthenticated remote DoS via `/shutdown` endpoint on signer's event listener - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` handles `POST /shutdown` on the same HTTP listener socket that receives `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` from the node, with no authentication, secret, or origin check whatsoever. Any remote party who can open a TCP connection to the signer's configured `endpoint` can send a single crafted HTTP request to unconditionally set `stop_signal` to `true`, terminating the signer's event-processing loop.

### Finding Description
The broken equality claimed by the question holds: the code asserts "a request that terminates the signer's `main_loop`" == "a request originating from the local trusted node process," but no such origin check exists in the code.

In `libsigner/src/events.rs`, `SignerEventReceiver::next_event` dispatches purely on `request.url()` and `request.method()`: [1](#0-0) 
When the URL is `/shutdown`, the handler immediately does `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` — there is no verification of a shared secret, no check of peer/source IP, no signature check, nothing distinguishing this request from any other inbound TCP client. This is the exact same `tiny_http`-backed `HttpServer` bound via `bind()` [2](#0-1)  that serves the legitimate node-originated `/stackerdb_chunks` events, so anyone who can reach that socket (the signer's configured RPC-facing `endpoint`) can hit `/shutdown` too.

Once `stop_signal` is `true`, `is_stopped()` returns `true` [3](#0-2) , `next_event` immediately short-circuits to `Err(EventError::Terminated)` on all subsequent polls [4](#0-3) , and `EventReceiver::main_loop` breaks out of its loop entirely [5](#0-4) . There is no reset path for `stop_signal` — it is a one-way `AtomicBool` — so the event receiver thread exits permanently and the signer stops receiving node events (block proposals, burn blocks, stackerdb chunks) for the remainder of the process's life, until the whole signer binary is restarted.

The legitimate use of this same endpoint is entirely intra-process: `SignerStopSignaler::send()` connects back to `self.local_addr` and POSTs `/shutdown` to itself purely to unblock the `recv()` call inside `next_event` (as commented, "We need to send actual data to trigger the event receiver") [6](#0-5) . Nothing in this design assumes or enforces that the sender must be the same process — the wire-level HTTP endpoint is indistinguishable from any other remote POST.

### Impact Explanation
A single unauthenticated remote HTTP POST permanently disables the signer's event ingestion pipeline (StackerDB chunks, block validation responses, burn block events, new block events all stop being received/forwarded), which is a Critical-category "remote crash/unauthenticated DoS from few messages" per the given severity taxonomy. The affected party is the individual stacks-signer instance operator; because signer participation is required for Nakamato/Nakamoto block signing, disabling one signer's event loop removes it from the active consensus-signing pool for that reward cycle. This is repeatable against any signer instance whose listener is remotely reachable, and requires no valid signature, no RPC secret, no stacker-db slot ownership — merely TCP reachability.

### Likelihood Explanation
No preconditions are needed beyond being able to open a TCP connection to the signer's bound `endpoint` and send a well-formed but otherwise ordinary HTTP `POST /shutdown` request — the same triviality as sending a `/stackerdb_chunks` POST. `SpawnedSigner::new` even logs a warning that communicating with the endpoint externally "could potentially expose sensitive data or functionalities to security risks if additional proper security checks are not integrated" [7](#0-6) , which is a tacit acknowledgment that this listener is not access-controlled at the application layer and its exposure is left entirely to network-level (firewall/binding) hardening by the operator, not the code itself.

### Recommendation
Require the shutdown request to be authenticated/local — e.g., only accept `/shutdown` from a loopback-verified caller, or protect it with a per-process random shared secret token generated at `bind()` time and checked in the handler before calling `stop_signal.store(...)`, rather than trusting the URL path alone.

### Proof of Concept
Rust test (parallel to the existing `test_simple_signer` pattern in `libsigner/src/tests/mod.rs`):
1. Create a `SignerEventReceiver::new(false)`, `bind()` it to `127.0.0.1:0` (or a fixed test port), and start `main_loop()` in a spawned thread.
2. From a separate thread simulating an unprivileged remote attacker, open a fresh `TcpStream::connect` to the bound address (no shared secret, no prior handshake) and write raw bytes: `"POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1:PORT\r\nConnection: close\r\nContent-Length: 4\r\nContent-Type: text/plain\r\n\r\npwnd"`.
3. Poll `event_receiver.is_stopped()` (or observe via a shared `Arc<AtomicBool>` clone) and assert it becomes `true` shortly after the write.
4. Assert that a subsequent call to `next_event()` (or the `main_loop` thread's join) returns/exits due to `EventError::Terminated`, confirming the event stream is dead — with no credentials ever presented by the "attacker" connection.

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

**File:** libsigner/src/events.rs (L376-396)
```rust
impl EventStopSignaler for SignerStopSignaler {
    #[cfg_attr(test, mutants::skip)]
    fn send(&mut self) {
        self.stop_signal.store(true, Ordering::SeqCst);
        // wake up the thread so the atomicbool can be checked
        // This makes me sad...but for now...it works.
        if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
            // We need to send actual data to trigger the event receiver
            let body = "Yo. Shut this shit down!".to_string();
            let req = format!(
                "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
                self.local_addr,
                body.len(),
                body
            );
            if let Err(e) = stream.write_all(req.as_bytes()) {
                error!("Failed to send shutdown request: {}", e);
            }
        }
    }
}
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L413-418)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
```

**File:** libsigner/src/events.rs (L437-446)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
```

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```

**File:** stacks-signer/src/lib.rs (L119-132)
```rust
impl<S: Signer<T> + Send + 'static, T: SignerEventTrait + 'static> SpawnedSigner<S, T> {
    /// Create a new spawned signer
    pub fn new(config: GlobalConfig) -> Self {
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
