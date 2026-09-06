### Title
Unauthenticated `POST /shutdown` on the signer's event-receiver HTTP socket permanently halts the signer's event loop - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` handles the `/shutdown` URL by unconditionally setting `stop_signal` and returning `Err(EventError::Terminated)`, with no authentication, secret, or origin check on the request. Any TCP client that can reach the signer's event-receiver listening address can send a single raw `POST /shutdown HTTP/1.1` request and permanently stop the signer's `main_loop`.

### Finding Description
`SignerEventReceiver::bind` opens a plain `tiny_http::HttpServer` on the configured `endpoint` address [1](#0-0) . `next_event` dispatches based solely on the request's `url()`, with no signature, secret, or peer-identity check anywhere in the function [2](#0-1) . The `/shutdown` branch performs no validation of the caller at all:

```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [3](#0-2) 

This code path is intended only to be triggered internally by `SignerStopSignaler::send`, which connects to `self.local_addr` and issues exactly this same request body/format to wake up the blocking `HttpServer::recv()` call [4](#0-3) . However, the handler itself cannot distinguish that legitimate internal caller from any other TCP client that connects to the same socket and sends the identical bytes — there is no secret token, HMAC, loopback-only check, or peer address allow-list applied before the `stop_signal.store(true, ...)` write.

Once `stop_signal` is set, `is_stopped()` returns `true` [5](#0-4) , and `main_loop` treats the returned `Err(EventError::Terminated)` as a normal termination and `break`s out of its loop [6](#0-5) , permanently ending event processing for that signer instance (StackerDB chunks, block proposals, burn blocks, new blocks) until the process is restarted.

### Impact Explanation
A single unauthenticated HTTP request causes the signer's event-receiver thread to exit its main loop and stop consuming further events (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`). This is a remote, single-message, unauthenticated denial-of-service against the signer's event pipeline — matching the "Critical: remote crash/unauthenticated DoS from few messages" category. Repeated on every signer that exposes this endpoint reachably, it could stall a chunk of the signer set's participation in block signing until manual restart.

### Likelihood Explanation
The precondition is that the attacker can open a TCP connection to the signer's configured event-receiver `endpoint` (the address the stacks-node event-observer posts to). This is operator-configured; if bound to a non-loopback interface (e.g., `0.0.0.0:<port>` or any externally routable address, which is common in multi-host or containerized deployments), it is remotely reachable by any unprivileged network party with no secret, no StackerDB slot, and no peer identity required. The attack requires exactly one crafted HTTP request and no retries, races, or special timing. No signature/secret verification exists in `next_event` for any of its routes, so the "guard" the audit rules ask about does not exist for `/shutdown`.

### Recommendation
Restrict the event-receiver `/shutdown` route so it can only be triggered by the process itself, e.g.:
- Bind the shutdown-signaling path to loopback only and verify `request.remote_addr()` is `127.0.0.1`/`::1` before honoring the request, or
- Require a per-process random shared secret (generated at `bind()` time and known only to `SignerStopSignaler`) to be presented in the request (e.g., as a header or path token) before setting `stop_signal`, or
- Replace the internal wake-up mechanism entirely with a non-network primitive (e.g., a self-pipe, condvar, or short poll timeout on `recv()`) so the HTTP listener never exposes a shutdown-capable route to the network at all.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a standalone integration test
use std::io::Write;
use std::net::{SocketAddr, TcpStream};
use std::thread;

#[test]
fn unauthenticated_shutdown_kills_main_loop() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> =
        SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let bound = receiver.bind(addr).unwrap();

    // Attacker connects directly, without any secret/auth, and never called get_stop_signaler.
    let handle = thread::spawn(move || {
        receiver.main_loop();
        receiver // return to check is_stopped after loop exit
    });

    let mut stream = TcpStream::connect(bound).unwrap();
    let body = "attacker-controlled";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let receiver_after = handle.join().unwrap();
    assert!(receiver_after.is_stopped()); // stop_signal now true from unauthenticated request
    // main_loop already returned -- event stream permanently halted
}
```
This reproduces the exact code path: the raw socket write reaches `next_event`'s `/shutdown` branch [3](#0-2) , setting `stop_signal` and returning `Err(EventError::Terminated)`, which `main_loop` treats as a normal exit condition [6](#0-5) .

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
```

**File:** libsigner/src/events.rs (L378-395)
```rust
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
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L413-422)
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

```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
