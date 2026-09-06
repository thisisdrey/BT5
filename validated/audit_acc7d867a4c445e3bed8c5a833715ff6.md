### Title
Unauthenticated `POST /shutdown` permanently kills the signer's event-receiver loop - (`libsigner/src/events.rs`)

### Finding Description
`SignerEventReceiver::next_event` dispatches purely on `request.url()` string equality after checking only that the method is `POST`, with no source authentication of any kind: `request.method() != &HttpMethod::Post` is the only gate before the URL match against `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`. [1](#0-0) 

The `/shutdown` branch unconditionally sets `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returns `Err(EventError::Terminated)` for any POST to that path, regardless of who sent it or what body it carries: [2](#0-1) 

This is in fact the exact mechanism the signer's own internal `SignerStopSignaler::send` uses to wake and stop the loop from the same process, by connecting to `self.local_addr` and issuing `POST /shutdown HTTP/1.1 ... Content-Length: ... <body>`: [3](#0-2) 

Because the HTTP listener created in `bind()` via `HttpServer::http(listener)` accepts on whatever socket address is configured (the endpoint is the signer's "event port", intended to receive callbacks from the node's event dispatcher) and there is no shared-secret, token, or peer/IP allow-list check in the request-handling path, any TCP peer that can reach that port can send this exact byte sequence and trigger the same termination path. Once triggered, `main_loop` (in the `EventReceiver` trait) sees `Err(EventError::Terminated)` and breaks out of the loop permanently, per: [4](#0-3) 

### Impact Explanation
A single unauthenticated POST request permanently terminates the signer's event-receiver thread, cutting the signer off from all future StackerDB chunks, block proposals, burn-block events, and new-block events delivered by the node's event dispatcher. This is a Critical single-message unauthenticated DoS against the transport layer that feeds the signer runloop — it does not corrupt consensus state but it can silently and permanently blind a signer to new work with no restart mechanism visible in this file. Repeating the request costs the attacker nothing (one TCP connection, no computation), and it works identically against every signer node whose event port is reachable.

### Likelihood Explanation
The only precondition is TCP reachability to the bound event-receiver address/port, which is exactly the class of "unprivileged attacker who can connect to a node's port and send arbitrary bytes" that is in scope. No secret, key, StackerDB slot, or peer identity is required — the handler performs zero authentication before honoring `/shutdown`. If the operator binds this listener to a non-localhost interface (the code imposes no restriction to loopback within `events.rs`), the attack is trivially exploitable by any remote host with network access to that port.

### Recommendation
Do not accept unauthenticated shutdown control over the same HTTP endpoint that receives untrusted event-dispatcher callbacks. At minimum: bind the event-receiver socket to loopback-only by default and document that binding elsewhere is unsafe; and/or require a shared secret / local-only source check (e.g., verify the peer socket address is loopback, or require a bearer token matching a locally-generated secret) before honoring `/shutdown`, decoupling the internal wake-up mechanism from an externally reachable, unauthenticated control path.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) — reproduces unauthenticated remote shutdown
#[test]
fn test_unauthenticated_shutdown_terminates_receiver() {
    use std::io::Write;
    use std::net::TcpStream;

    let mut receiver: SignerEventReceiver<v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: raw socket, no auth, no secret
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nContent-Length: {}\r\n\r\n{}",
        addr, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Next call to next_event() observes termination triggered by the unauthenticated request
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());
}
```
This exercises the exact branch at `libsigner/src/events.rs:443-445`, confirming that an unauthenticated remote `POST /shutdown` sets `stop_signal` and causes `next_event`/`main_loop` to terminate permanently.

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

**File:** libsigner/src/events.rs (L430-446)
```rust
            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
            debug!("Processing {} event", request.url());
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
