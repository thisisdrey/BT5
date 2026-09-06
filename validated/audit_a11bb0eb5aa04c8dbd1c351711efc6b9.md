This is a documented, known design characteristic (also self-referenced in the code's own `SignerStopSignaler::send` implementation, which issues exactly this `POST /shutdown` to trigger shutdown). The code confirms the claim precisely, and this endpoint is the node's own internal shutdown mechanism, gated only by network reachability, not by any secret.

### Title
Unauthenticated remote `POST /shutdown` permanently halts signer event processing - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches on `request.url()` and only requires `request.method() == POST` generically before matching `/shutdown`, with no secret, token, or origin check gating that specific path. Any TCP peer that can reach the bound event-receiver address can send a bare `POST /shutdown` and permanently set `stop_signal`, causing `main_loop` to exit for good.

### Finding Description
In `next_event` [1](#0-0) , the only checks performed before dispatching by URL are: (1) `/status` is special-cased and bypasses the method check entirely, and (2) all other paths require `request.method() == HttpMethod::Post` [2](#0-1) . There is no authentication token, shared secret, or source-address restriction anywhere in this function or in `HttpServer::bind`/`recv`. When `request.url() == "/shutdown"`, the handler unconditionally executes `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` [3](#0-2) . This is precisely the same call the legitimate internal `SignerStopSignaler::send` uses to shut down the receiver from the same process [4](#0-3) , but nothing distinguishes a legitimate local caller from an arbitrary remote TCP client sending the same bytes. The `Err(EventError::Terminated)` propagates out of `next_event` into `main_loop`'s `match`, which explicitly treats `Terminated` as an intentional exit condition and `break`s the loop [5](#0-4) . Once `main_loop` exits, the signer's event-processing thread never resumes; the `stop_signal` `AtomicBool` is never reset anywhere in this file.

### Impact Explanation
A single unauthenticated `POST /shutdown` request permanently terminates the signer's event-receiver `main_loop`, meaning `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events dispatched from the node's event observer will no longer be processed. This is a remote unauthenticated denial-of-service triggered by a single crafted message, matching the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
This requires only that the attacker can open a TCP connection to the bound event-receiver socket and send one HTTP POST request — no secret, peer identity, StackerDB slot ownership, or any other privileged credential is needed. The question's own precondition frames this as remote reachability being contingent on the operator binding this listener to a non-loopback interface; if the deployment binds only to loopback (as is the conventional expectation for an "event observer" endpoint meant to be reached solely by the local node), the attack requires local access, which is out of scope for this audit's unprivileged-remote-attacker model. The vulnerability in the code itself (lack of any auth check specific to `/shutdown`) is real, but its exploitability by a remote unprivileged attacker is entirely conditional on operator misconfiguration of the bind address rather than a flaw reachable purely through the P2P/RPC network paths in scope.

### Recommendation
Add an authentication check (e.g., a shared secret header/token configured between the node's event dispatcher and the signer, or restrict acceptance to loopback/configured trusted source addresses) before honoring `/shutdown`, and consider making `bind()` enforce or warn when the configured listener address is non-loopback.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) - conceptual PoC
#[test]
fn test_unauthenticated_shutdown() {
    let mut receiver: SignerEventReceiver<v0::messages::SignerMessage> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    // Simulate a remote attacker (any TCP client, no secret) sending raw bytes:
    std::thread::spawn(move || {
        let mut stream = std::net::TcpStream::connect(addr).unwrap();
        let req = "POST /shutdown HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n";
        stream.write_all(req.as_bytes()).unwrap();
    });
    // next_event() should return Err(EventError::Terminated) and stop_signal should now be true
    let err = receiver.next_event().unwrap_err();
    assert!(matches!(err, EventError::Terminated));
    assert!(receiver.is_stopped());
}
```
This confirms that a bare, unauthenticated `POST /shutdown` sets `stop_signal` and causes `next_event`/`main_loop` termination, per [3](#0-2) . Note: per audit scope, remote reachability must be confirmed against the actual deployed bind address (loopback vs. non-loopback) for this to qualify as remotely exploitable by an unprivileged party.

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
