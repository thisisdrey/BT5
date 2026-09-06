### Title
Unauthenticated `/shutdown` POST permanently halts signer event processing - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` matches the raw HTTP request URL against `/shutdown` and unconditionally sets `stop_signal` to `true` with no signature, secret, or peer-identity check. Any TCP client that can reach the bound event-receiver socket can send a single crafted POST to permanently stop the signer's event loop.

### Finding Description
The intended invariant is that only the node process that owns this signer (via its internal `SignerStopSignaler::send`) should be able to trigger a shutdown of the event receiver. In `next_event`, the dispatch logic is: [1](#0-0) 
This branch is reached after only checking the HTTP method is `POST` [2](#0-1)  — there is no signature verification, no shared-secret/header check, and no peer-identity check anywhere in `next_event` before this match, unlike the legitimate stop path which is just `TcpStream::connect` + a hardcoded plaintext body with no credential at all [3](#0-2) . Once `stop_signal` is set, `is_stopped()` returns true [4](#0-3)  and `main_loop` exits on its next iteration without ever calling `forward_event` again [5](#0-4) . The code's own comment on the legitimate `SpawnedSigner::new` acknowledges this exposure, warning against exposing the endpoint to untrusted networks [6](#0-5) .

### Impact Explanation
Any attacker with plain TCP connectivity to the configured signer `endpoint` socket can permanently disable that signer's event processing with a single unauthenticated HTTP POST to `/shutdown`. The signer will no longer forward StackerDB chunk events, block-validation responses, burn-block events, or new-block events to its runloop, silencing that signer node from further participation until manually restarted — a critical, single-message, unauthenticated denial of service, matching the "remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The only precondition is TCP reachability to the address passed to `EventReceiver::bind` (the signer's `endpoint`, e.g. `127.0.0.1:30000` in sample configs, but operator-configurable and not required to be loopback-only). No secret, key, peer identity, or StackerDB slot ownership is required — the attacker cost is a single raw HTTP request, and the effect is deterministic and repeatable against any reachable instance.

### Recommendation
Require authentication before honoring `/shutdown` (and ideally all POST routes) in `SignerEventReceiver::next_event` — e.g., validate a shared secret/HMAC header, restrict the stop signal to a loopback-only listener plus a local control channel, or replace the HTTP-based stop signal with an in-process channel/`TcpStream` bound strictly to `127.0.0.1` and never exposed on a public interface, with the check enforced before `stop_signal.store(...)` is called.

### Proof of Concept
1. Bind a `SignerEventReceiver<T>` via `Signer::spawn` on a test address as in `test_status_endpoint` [7](#0-6) .
2. From a separate "foreign" thread, open a bare `TcpStream::connect(endpoint)` and write raw bytes: `POST /shutdown HTTP/1.1\r\nHost: <endpoint>\r\nContent-Length: 0\r\n\r\n` (mirroring the format in `SignerStopSignaler::send` but with no legitimate caller involved) [8](#0-7) .
3. Assert that `event_receiver.is_stopped()` becomes `true` and that `main_loop` exits (or, using `RunningSigner`, assert `.join()` completes) without any `forward_event` call having occurred for legitimate events sent afterward.

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

**File:** libsigner/src/events.rs (L430-435)
```rust
            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** libsigner/src/events.rs (L461-464)
```rust
    /// Determine if the receiver is hung up
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

**File:** libsigner/src/tests/mod.rs (L199-222)
```rust
#[test]
fn test_status_endpoint() {
    let ev = SignerEventReceiver::new(false);
    let (res_send, _res_recv) = channel();
    let max_events = 1;
    let mut signer = Signer::new(SimpleRunLoop::new(max_events), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:31000".parse().unwrap();

    // Spawn the signer first so the HTTP server is listening before the mock client connects
    let running_signer = signer.spawn(endpoint).unwrap();

    // simulate a node that's trying to push data
    let mock_stacks_node = thread::spawn(move || {
        let mut sock = TcpStream::connect(endpoint).unwrap();
        let req = format!("GET /status HTTP/1.1\r\nHost: {endpoint}\r\nConnection: close\r\n\r\n");

        sock.write_all(req.as_bytes()).unwrap();
        let mut buf = [0; 128];
        let _ = sock.read(&mut buf).unwrap();
        let res_str = std::str::from_utf8(&buf).unwrap();
        let expected_status_res = "HTTP/1.1 200 OK\r\n";
        assert_eq!(expected_status_res, &res_str[..expected_status_res.len()]);
        sock.flush().unwrap();
    });
```
