### Title
Unauthenticated `/shutdown` POST permanently halts the signer's event receiver — ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` treats any HTTP POST to `/shutdown` on its bound event socket as authoritative, unconditionally setting `stop_signal` and returning `Err(EventError::Terminated)`. There is no verification that the request originated from the node's own `SignerStopSignaler`, so any TCP client that can reach the socket can permanently stop the signer's `main_loop` event processing with a single request.

### Finding Description
`SignerStopSignaler::send` (the *intended* caller) shuts the receiver down by opening a `TcpStream` to `self.local_addr` and writing a raw `POST /shutdown HTTP/1.1` request body [1](#0-0) . The receiver side, `SignerEventReceiver::next_event`, dispatches purely on the HTTP method and URL string, with a dedicated branch:

```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [2](#0-1) 

No source-address check, secret/token, or any other credential distinguishes a legitimate stop request from an arbitrary one — the handler is reached by any TCP peer that can complete an HTTP request against the bound `HttpServer` (via `tiny_http`) at `local_addr` [3](#0-2) . Once `stop_signal` is set, `is_stopped()` returns `true` on every subsequent call, and `Err(EventError::Terminated)` unwinds straight to `EventReceiver::main_loop`, which breaks the loop permanently:

```
Err(EventError::Terminated) => {
    info!("Caught termination signal");
    break;
}
``` [4](#0-3) 

The claimed equality — "only the node's own `SignerStopSignaler` should be able to author a termination" — is indeed broken: the handler authenticates the *message shape* (method + URL), not the *origin*. Any of the other protected event routes (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) at least require a valid JSON body deserializable into a typed event, but `/shutdown` requires nothing beyond the URL match.

### Impact Explanation
Any party able to open a TCP connection to the signer's bound event-receiver socket can permanently disable that signer's event pipeline (StackerDB chunk delivery, block-validation responses, burn-block/Stacks-block notifications) with a single unauthenticated HTTP request. Because `main_loop` exits entirely rather than merely dropping one event, the signer stops observing and reacting to state (block proposals, its own StackerDB slot updates, etc.) until the signer process is restarted — a persistent, repeatable, single-message DoS against a specific signer's participation in consensus signing. This matches the "Critical – remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Exploitability requires only that the attacker can reach the TCP port the signer bound for its event-receiver server (the `endpoint` configured in the signer's config, which the node's event-observer posts to). No secret, peer key, or StackerDB slot ownership is needed — a bare `POST /shutdown` with any body suffices, and the request costs a single TCP connection. The realistic constraint is network reachability: if the operator binds the event endpoint to a non-loopback/public interface (a common misconfiguration, especially in containerized deployments where node and signer are on different hosts), the endpoint is remotely reachable by any unprivileged attacker. If strictly bound to loopback and firewalled, remote exploitation is prevented at the network layer rather than by any code-level guard — the code itself performs no origin check either way.

### Recommendation
Do not trust the URL alone to authorize termination. Add an out-of-band authentication mechanism for the stop signal — e.g., a shared secret/token embedded in the shutdown request (checked before setting `stop_signal`), or switch to an in-process/loopback-only mechanism (a Unix domain socket, or an internal channel/self-pipe) instead of an HTTP endpoint reachable over the network. At minimum, restrict `/shutdown` handling to requests whose peer address matches `127.0.0.1`/the configured trusted node address, verified via the underlying stream rather than relying on `tiny_http`'s parsed URL.

### Proof of Concept
```rust
// libsigner/src/tests/mod.rs (conceptual addition)
#[test]
fn unauthenticated_shutdown_stops_receiver() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: raw TCP client, no relation to the node's SignerStopSignaler
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "arbitrary";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        addr, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Assert: next_event now reports permanent termination
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());
}
```
This confirms an attacker with no credentials, no peer relationship, and no admin role can silence the signer's event stream with a single crafted POST.

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

**File:** libsigner/src/events.rs (L401-408)
```rust
    /// Start listening on the given socket address.
    /// Returns the address that was bound.
    /// Errors out if bind(2) fails
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
