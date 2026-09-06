This confirms `auth_password` in `stacks-signer/src/config.rs` is only used by `stacks-signer/src/client/stacks_client.rs` — i.e., it authenticates the *outgoing* signer→node RPC calls, not incoming requests to the signer's own event-receiver HTTP listener. `SignerEventReceiver::next_event` (`libsigner/src/events.rs`) performs no `Authorization`/token check whatsoever on incoming requests. [1](#0-0) 

### Title
Unauthenticated remote `/shutdown` request permanently halts the signer's event receiver - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` treats any HTTP `POST /shutdown` on its bound listener as a valid termination command, setting `stop_signal` to `true` and returning `EventError::Terminated`, with no signature, token, or `auth_password` verification of the caller. Since the event-receiver's `HttpServer` accepts connections from any TCP peer that can reach the configured `endpoint` (e.g. `0.0.0.0:30000` per the sample mainnet-signer config), any unprivileged remote party able to open a TCP connection to that address can permanently stop the signer's `main_loop`.

### Finding Description
`SignerStopSignaler::send` (the *legitimate* internal caller) builds a raw `POST /shutdown HTTP/1.1` request and writes it to a `TcpStream` connected to `self.local_addr` [2](#0-1) . On the receiving side, `SignerEventReceiver::next_event` special-cases the URL comparison:

```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [3](#0-2) 

There is no check of any header, token, or `auth_password`/`auth_token` value before performing this store — `EventReceiver::bind` simply opens an `HttpServer::http(listener)` socket with no TLS or authentication layer [4](#0-3) . The only authentication mechanism present in the signer codebase, `auth_password` (config field in `stacks-signer/src/config.rs`), is used exclusively by `stacks-signer/src/client/stacks_client.rs` for the signer's *outgoing* RPC calls to the node — it is never consulted by `SignerEventReceiver`/`next_event`. The `main_loop` in `libsigner/src/runloop.rs` treats `Err(EventError::Terminated)` as a clean exit condition, breaking the loop [5](#0-4) , so once `stop_signal` is set, the receiver thread exits permanently and `is_stopped()` will also cause any subsequent loop iteration to exit immediately.

The signer binary's own startup code acknowledges this exposure directly:
```
warn!(
    "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
    It's important to exercise caution if you are communicating with an external node, \
    as this could potentially expose sensitive data or functionalities to security risks \
    if additional proper security checks are not integrated in place. ..."
);
``` [6](#0-5) 

This confirms the receiver has no authentication for inbound requests, and operators are expected to firewall the endpoint rather than the code enforcing any check.

### Impact Explanation
A single crafted HTTP request permanently halts the signer's event-receiving thread, causing the signer to stop processing StackerDB chunks, block proposals, and burn-block events indefinitely — a remote unauthenticated denial-of-service against a signer process. This matches the "Critical - remote crash/unauthenticated DoS from few messages" category. Repeated against multiple signers reachable on a public/insufficiently firewalled interface, this could disable a meaningful fraction of the signer set's ability to participate in block signing.

### Likelihood Explanation
The only precondition is TCP reachability of the signer's configured `endpoint` — which sample configs sometimes list as `0.0.0.0:30000` (`sample/conf/mainnet-miner-conf.toml` events_observer section shows `127.0.0.1:30000`, but the signer's own `endpoint` binds to whatever address is configured, and there is no code-level restriction to loopback). Attacker cost is a single raw HTTP request with no credentials, no cryptographic material, and no privileged role — well within the "unprivileged remote attacker" threat model, assuming the operator has not firewalled the port off from the public network. The vulnerability is fully repeatable (one message per signer process) and requires no elevated position (miner, peer, or slot holder) — just network reachability.

### Recommendation
Require authentication (e.g. reuse the shared secret/token mechanism, such as comparing an `Authorization` header against the configured `auth_password`) before honoring `/shutdown` (and any other mutating endpoint) in `SignerEventReceiver::next_event`, or bind the event-receiver listener to loopback/a mutually-authenticated channel by default and reject non-local connections.

### Proof of Concept
```rust
// libsigner/src/tests/mod.rs (new test)
#[test]
fn test_unauthenticated_shutdown() {
    let ev = SignerEventReceiver::<SignerMessage>::new(false);
    let (res_send, _res_recv) = channel();
    let mut signer = Signer::new(SimpleRunLoop::new(0), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:30099".parse().unwrap();
    let running = signer.spawn(endpoint).unwrap();

    // Attacker: no credentials, just a raw TCP connection.
    let mut sock = TcpStream::connect(endpoint).unwrap();
    let body = "pwn";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        endpoint, body.len(), body
    );
    sock.write_all(req.as_bytes()).unwrap();

    // main_loop should exit on its own due to the unauthenticated shutdown.
    let result = running.join(); // returns without ever calling stop_signal.send()
    assert!(result.is_none() /* or however SimpleRunLoop signals termination */);
}
```
The assertion point is that `event_receiver.stop_signal` (an `Arc<AtomicBool>`) becomes `true` via the unauthenticated request at `libsigner/src/events.rs:444`, causing `main_loop` (`libsigner/src/events.rs:296-300`) to break out and the event thread to exit — without any call to the legitimate `SignerStopSignaler::send()` from the runloop side.

### Citations

**File:** libsigner/src/events.rs (L290-300)
```rust
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
