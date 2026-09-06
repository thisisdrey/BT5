### Title
Unauthenticated remote shutdown of signer event receiver via `POST /shutdown` - (`File: libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` treats any HTTP `POST /shutdown` request received on its bound socket as an authoritative termination signal, with no authentication, shared secret, or loopback restriction. Any remote party able to reach the signer's event-receiver TCP port can send this request to permanently halt the signer's event ingestion loop.

### Finding Description
The intended invariant is that only the signer process itself, via `SignerStopSignaler::send`, should be able to stop the event receiver's `main_loop`. `SignerStopSignaler::send` implements this by setting the shared `stop_signal` `AtomicBool` and then connecting to its own bound address to send a `POST /shutdown` request purely to wake up the blocking `http_server.recv()` call so the atomic flag gets checked promptly: [1](#0-0) 

However, the actual termination logic lives in `next_event`'s URL dispatch, which is reached by *any* incoming HTTP request matching `/shutdown`, not just the self-directed wakeup connection: [2](#0-1) 

Specifically, lines 443-445 unconditionally set `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and return `Err(EventError::Terminated)` for any request whose URL is `/shutdown`, regardless of the TCP peer's identity, without checking any header, secret, or signature. There is no mechanism distinguishing "the local stop-signaler's wakeup connection" from "an arbitrary remote POST to /shutdown" — the equality the code implicitly assumes (any `/shutdown` request == an authorized self-shutdown) does not hold. `EventReceiver::main_loop` then observes `Err(EventError::Terminated)` and breaks out of the loop: [3](#0-2) 

None of the other endpoints (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) perform any authentication either, but `/shutdown` is unique in that it requires no valid payload at all — an empty or arbitrary body suffices to trip the termination path.

### Impact Explanation
A single crafted HTTP request permanently disables the signer's event-processing loop: `is_stopped()` becomes `true`, `main_loop` exits, and the signer stops receiving StackerDB chunk events, block validation responses, and burn/stacks block events from the node. This is a remote, unauthenticated, single-message denial of service against the signer process, matching the "Critical - remote crash/unauthenticated DoS from few messages" category. The affected party is whichever signer's event-receiver socket is reachable by the attacker.

### Likelihood Explanation
The only precondition is TCP reachability to the signer's bound event-receiver `SocketAddr` (the `endpoint` configured in `stacks-signer`), which is not restricted to loopback anywhere in this code path — `bind` simply calls `HttpServer::http(listener)` on whatever address is configured. If an operator binds this endpoint to a non-loopback interface (e.g., `0.0.0.0` or a LAN address, as is common in multi-host node/signer deployments), the attack requires no credentials, no valid StackerDB slot, and no signature — just one TCP connection and one HTTP request. It is trivially repeatable.

### Recommendation
Distinguish the self-directed shutdown wakeup from externally-originated requests: e.g., have `SignerStopSignaler::send` include a per-process random secret/token (generated at `bind` time and known only to the in-process `SignerEventReceiver`/`SignerStopSignaler` pair) in the `/shutdown` request, and have `next_event` validate that token before honoring the shutdown, or restrict the endpoint to require the connection to originate from `127.0.0.1`/the local host in addition to token validation. At minimum, verify the request's source address is loopback before acting on `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
#[test]
fn unauthenticated_remote_shutdown() {
    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: open a TCP connection and send a raw shutdown request,
    // with no secret/signature/loopback check enforced anywhere.
    let attacker_thread = std::thread::spawn(move || {
        let mut stream = std::net::TcpStream::connect(addr).unwrap();
        let body = "attacker payload";
        let req = format!(
            "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
            addr, body.len(), body
        );
        stream.write_all(req.as_bytes()).unwrap();
    });

    let result = receiver.next_event();
    attacker_thread.join().unwrap();

    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());
}
```
This confirms `next_event` at `libsigner/src/events.rs:443-445` accepts an unauthenticated `POST /shutdown` from any TCP peer and flips `stop_signal`, terminating the event receiver.

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
