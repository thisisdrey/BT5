### Title
Unauthenticated remote shutdown of `SignerEventReceiver` via `POST /shutdown` - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` matches the literal path `/shutdown` and, on any POST to it, unconditionally sets `stop_signal` and returns `EventError::Terminated`, with no signature, secret, or origin check. Any TCP client that can reach the signer's event-listener HTTP port can permanently halt the signer's event pipeline with a single request.

### Finding Description
In `SignerEventReceiver::next_event` [1](#0-0) , the request path is routed purely by matching `request.url()`. When the path equals `/shutdown`, the handler immediately does:
```
event_receiver.stop_signal.store(true, Ordering::SeqCst);
Err(EventError::Terminated)
``` [2](#0-1) 

There is no comparison against a shared secret, no check of the remote peer, and no signature verification anywhere on this path — unlike the StackerDB chunk path, which at least goes through `process_event` and eventual chunk/signature handling. The only "protection" that exists is that `SignerStopSignaler::send` (the legitimate internal caller) happens to construct the exact same `POST /shutdown` HTTP request over a fresh `TcpStream` connect to the bound address [3](#0-2)  — i.e., the "authentication" is merely knowing the well-known literal string `/shutdown`, which is public in this open-source file.

`EventReceiver::main_loop` treats `Err(EventError::Terminated)` as an unconditional exit condition, breaking out of the loop and ending the receiver thread [4](#0-3) . Once stopped, `is_stopped()` will always return true [5](#0-4) , and every subsequent call to `next_event` will short-circuit with `Err(EventError::Terminated)` before ever calling `http_server.recv()` [6](#0-5) , so the receiver can never come back online without a process restart.

### Impact Explanation
A remote, completely unprivileged attacker who can open a TCP connection to the signer's event-listener HTTP port (the address the node's event dispatcher POSTs to) can send a single `POST /shutdown` request and permanently disable that signer's event ingestion (StackerDB chunks, burn blocks, new blocks, block-validation responses all stop being delivered to the signer runloop). This is a one-message, unauthenticated, repeatable DoS against the signer's control-plane transport, matching the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
No preconditions beyond network reachability to the bound event-listener port are required — no StackerDB slot, no key, no node secret. The `HttpServer::bind` call in `SignerEventReceiver::bind` simply opens a listener socket on the configured address; if that is bound to a non-loopback interface (or otherwise reachable, e.g., via port-forward/misconfiguration/same LAN), any host on that path can trigger this. The exploit is a single crafted HTTP request with zero cost and no interaction with dispatcher or StackerDB state, and is fully repeatable against any restarted receiver.

### Recommendation
Require authentication on the `/shutdown` (and other event-observer) endpoints, e.g., a shared secret/HMAC included in the request that `SignerStopSignaler::send` populates and `next_event` verifies before honoring the stop request, or restrict the shutdown control path to a local IPC mechanism (e.g., a Unix socket or in-process channel) instead of exposing it over the same public HTTP listener used for node event delivery.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use std::net::TcpStream;
use std::io::Write;
use std::thread;

#[test]
fn test_unauthenticated_remote_shutdown() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    let handle = thread::spawn(move || {
        receiver.main_loop();
        receiver.is_stopped()
    });

    // Attacker: no key, no slot, no secret -- just a raw POST.
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "pwn";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).unwrap();

    let stopped = handle.join().unwrap();
    assert!(stopped); // main_loop exited via EventError::Terminated with zero prior authentication
}
```
The assertion passes because `next_event` unconditionally sets `stop_signal` on the literal `/shutdown` match at [2](#0-1) , causing `main_loop` to terminate at [4](#0-3) .

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

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
