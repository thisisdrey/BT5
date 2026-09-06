### Title
Unauthenticated remote shutdown of signer event loop via `/shutdown` HTTP endpoint - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event()` handles a `POST /shutdown` HTTP request by unconditionally setting `stop_signal` to `true` and returning `Err(EventError::Terminated)`, with no authentication, secret, or origin check on the caller. Any party able to open a TCP connection to the signer's event-listener port (bound via `SignerEventReceiver::bind`) can send this request and permanently halt the signer's event `main_loop`.

### Finding Description
The intended invariant is that only the in-process `SignerStopSignaler::send()` (a trusted, same-process caller holding the shared `Arc<AtomicBool>`) should be able to trigger termination of the event receiver. In practice, the stop signal is triggered purely by matching the HTTP request path: [1](#0-0) 

This branch is reached in `next_event()` after only checking `request.method() == POST`, with no signature, token, or loopback-only enforcement: [2](#0-1) 

`SignerStopSignaler::send()` itself constructs the exact same kind of request from a plain `TcpStream::connect` — it is not a special authenticated channel, just a raw HTTP POST to `/shutdown`: [3](#0-2) 

Any remote party who can reach the bound address (`SignerEventReceiver::bind`, called from `EventReceiver::bind`) can open its own `TcpStream`, send `POST /shutdown HTTP/1.1` with an arbitrary body, and cause the exact same state transition: `stop_signal.store(true, Ordering::SeqCst)` followed by `Err(EventError::Terminated)`. [4](#0-3) 

The `main_loop` driving the receiver treats `EventError::Terminated` as a clean exit signal and breaks out permanently: [5](#0-4) 

Once `stop_signal` is `true`, `is_stopped()` will always return `true` (it's a simple atomic load with no reset path), so the loop cannot resume: [6](#0-5) 

No existing guard prevents this: there is no shared secret, no signature check, no check of remote peer address against loopback, and no distinction between the "internal" stop request and an externally supplied one — the path string comparison is the entire "authentication."

### Impact Explanation
An attacker who can reach the signer's event-listener TCP port can send a single crafted HTTP request to permanently disable that signer's event-processing loop. After this, the signer stops receiving StackerDB chunks, burn block events, and new block events from the node entirely, requiring a process restart to recover. This is a single-message, unauthenticated, remote denial of service against a critical signer component — matching the "Critical: remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The only precondition is TCP reachability to the address that `SignerEventReceiver::bind` is configured to listen on. This is a config-controlled bind address in `stacks-signer`; if bound to a non-loopback interface (e.g., `0.0.0.0` or a LAN/public address, which is a legitimate operational configuration when the event dispatcher and signer run on different hosts), the endpoint is remotely reachable with no credentials, no valid slot ownership, and no prior interaction with the protocol. The attack requires exactly one crafted HTTP request and is trivially repeatable.

### Recommendation
Do not expose stop control over the same public HTTP interface used by the node's event dispatcher without authentication. Options: (1) gate `/shutdown` behind a per-process random shared secret known only to the in-process `SignerStopSignaler` (e.g., include it in the body/header and verify server-side before honoring the request); (2) bind the internal stop-signal mechanism to a separate loopback-only socket, or use an in-process channel/pipe instead of a network round-trip through the public listener; (3) at minimum, verify `request.remote_addr()` is loopback before honoring `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (or a new integration test)
use std::net::{TcpStream, SocketAddr};
use std::io::Write;
use std::thread;

#[test]
fn test_unauthenticated_remote_shutdown() {
    let mut receiver: SignerEventReceiver<SomeSignerEventTraitImpl> =
        SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let bound_addr = receiver.bind(addr).unwrap();

    // Attacker: independent TcpStream, NOT via SignerStopSignaler::send()
    let attacker_thread = thread::spawn(move || {
        let mut stream = TcpStream::connect(bound_addr).unwrap();
        let body = "attacker-controlled body";
        let req = format!(
            "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
            bound_addr, body.len(), body
        );
        stream.write_all(req.as_bytes()).unwrap();
    });

    // Server side: process one event; it should be Err(Terminated)
    let result = receiver.next_event();
    attacker_thread.join().unwrap();

    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());

    // Further calls remain terminated permanently
    let result2 = receiver.next_event();
    assert!(matches!(result2, Err(EventError::Terminated)));
}
```
This demonstrates that a party other than the legitimate `SignerStopSignaler` (which normally holds the shared `Arc<AtomicBool>` in-process) can trigger the exact same termination behavior purely via the network, confirming the unauthenticated DoS.

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

**File:** libsigner/src/events.rs (L413-436)
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

            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
            debug!("Processing {} event", request.url());
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
