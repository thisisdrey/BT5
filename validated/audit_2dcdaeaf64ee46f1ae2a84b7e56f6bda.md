### Title
Unauthenticated `/shutdown` HTTP request permanently kills the signer's event-receiver loop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` treats any `POST /shutdown` request as equivalent to the node's own internal stop signal, with no check of origin, secret, or body content. Any TCP client that can reach the signer's event-receiver listening port can terminate the signer's event loop with a single crafted HTTP request, causing unauthenticated, repeatable denial-of-service.

### Finding Description
The intended equality is "party allowed to halt the event loop == the signer process itself (via `SignerStopSignaler::send`)". That equality is broken: `next_event` matches purely on `request.url() == "/shutdown"` [1](#0-0) , unconditionally executing `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returning `Err(EventError::Terminated)`. There is no verification of the request's source address, a shared secret, or any token proving the sender is the node's own stop signaler.

`SignerStopSignaler::send` itself provides no real authentication either — it simply opens a plain `TcpStream::connect` from an arbitrary ephemeral port and sends a hardcoded HTTP request body (`"Yo. Shut this shit down!"`) to `/shutdown` [2](#0-1) . Nothing in this payload is secret or unguessable — the URL path and method are the entire "authentication."

Once `next_event` returns `Err(EventError::Terminated)`, `EventReceiver::main_loop` breaks out of its loop permanently: [3](#0-2) . This is not a recoverable error like `UnrecognizedEvent`; the receiver thread exits and stops accepting `stackerdb_chunks`, `proposal_response`, `new_burn_block`, and `new_block` events from the node indefinitely, until the signer process is manually restarted.

No other guard exists on this endpoint. Every branch in `next_event` (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`) is dispatched purely by URL string match with no authentication whatsoever [4](#0-3) . The signer binary does emit a startup warning acknowledging exposure risk if communicating with an external node [5](#0-4) , but this is only a documentation note, not a technical mitigation, and does not prevent the described request from being accepted and processed.

### Impact Explanation
Any remote TCP client able to reach the signer's event-receiver bind address (the port configured via `config.endpoint`, which the node's event dispatcher connects to) can send a single `POST /shutdown HTTP/1.1` request with an arbitrary body and permanently stop the signer's event-processing thread. This is an unauthenticated, single-message, repeatable denial-of-service against the signer, matching the Critical category ("remote crash/unauthenticated DoS from few messages"). The signer stops observing StackerDB chunks, burn blocks, new blocks, and block-validation responses from the node, effectively taking the signer offline until manually restarted.

### Likelihood Explanation
The attacker needs only network reachability to the signer's event-receiver TCP port and the ability to send raw bytes — no secret, no valid peer key, no StackerDB slot ownership, and no special role are required. The request is trivial to construct (a fixed HTTP method/URL), fully deterministic, and repeatable at will, so any misconfiguration that exposes this port beyond localhost/the node itself is immediately and completely exploitable.

### Recommendation
Require the event-receiver to authenticate that a `/shutdown` (and ideally all) request originates from the trusted node, e.g., via a shared secret/token embedded in the request (checked against a value configured out-of-band), or restrict acceptance to loopback/allow-listed source addresses, rather than trusting the bare URL path.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use std::io::Write;
use std::net::TcpStream;

#[test]
fn unauthenticated_shutdown_stops_receiver() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: bare TcpStream, no relation to SignerStopSignaler
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "arbitrary";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped()); // signal set by an unauthenticated remote party
}
```

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
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

**File:** libsigner/src/events.rs (L413-458)
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
                process_event::<T, StacksBlockEvent>(request)
            } else {
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
```

**File:** stacks-signer/src/lib.rs (L125-132)
```rust
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
