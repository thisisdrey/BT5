### Title
Unauthenticated remote `/shutdown` POST permanently halts signer event ingestion - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event()` treats any HTTP POST to `/shutdown` on the signer's bound event-listener socket as a trusted stop signal, setting `stop_signal` and returning `EventError::Terminated` with no check on the sender's identity. The signer's `bind_addr` is configured to accept connections from the Stacks node (typically reachable on the local network/loopback depending on deployment), and anyone who can reach that socket can send the exact same bytes `SignerStopSignaler::send()` constructs to halt the signer's event thread permanently.

### Finding Description
The intended invariant is that only the signer's own `SignerStopSignaler` (obtained via `get_stop_signaler()` and held internally by the runloop/signal-handler code) can cause `is_stopped()` to become true. `SignerStopSignaler::send()` implements this by literally opening a `TcpStream` to `self.local_addr` and writing raw HTTP bytes: [1](#0-0) 

On the receiving side, `next_event()` dispatches purely on `request.url()` with no signature, token, or peer-identity check: [2](#0-1) 

Any TCP client that can connect to the bound `tiny_http` server and send `POST /shutdown HTTP/1.1` with any body will hit this branch, since the handler only inspects `request.url()` and `request.method()`, never the origin address or any secret. The `bind()` call opens a plain HTTP listener with no auth layer at all: [3](#0-2) 

This breaks the equality the question describes: "the party that stops the receiver == the signer's own stop-signaler" — any remote peer reaching the port is equally capable of stopping it. Once `stop_signal` is set, `main_loop()` observes `Terminated` and breaks out permanently: [4](#0-3) 

There is no re-arm/reset path once `stop_signal` is set to `true` — it is a one-way flag stored via `AtomicBool`, so the event thread exits for good after a single crafted message.

### Impact Explanation
A single unauthenticated POST permanently stops the signer's event-receiver thread, cutting the signer off from all future StackerDB chunk events, block-proposal validation responses, and burn/Stacks block events pushed by the node. This is a Critical unauthenticated remote DoS against an individual signer node: the signer stops participating in signing rounds (no more `next_event()` results ever reach the runloop), degrading the network's threshold-signing availability if enough signers are hit, from one crafted TCP message per node, fully repeatable against every reachable signer instance.

### Likelihood Explanation
Preconditions: the attacker needs only TCP reachability to the signer's configured event-listener `bind_addr`/port — no RPC secret, no StackerDB slot ownership, no peer key, no privileged role. Attacker cost is a single raw TCP connect and a ~150-byte HTTP request; the exploit is deterministic and repeatable at will. The severity/likelihood is bounded by deployment: if the signer's event endpoint is bound to loopback only and reachable exclusively from its co-located node process on a trusted local link, external network attackers cannot reach it; if it is bound to a routable/LAN address (common in split node/signer deployments, e.g. `0.0.0.0` or a non-loopback address in `stacks-signer` config), the port is remotely reachable by anyone with network access to it. I could not fully verify from the indexed code whether the default/deployed configuration ever binds this endpoint to a non-loopback interface — `stacks-signer/src/config.rs`'s `endpoint` field defines the bind address but its default value and typical operator deployment guidance were not retrieved in this session, so likelihood scoring in real deployments carries that caveat.

### Recommendation
Do not rely on network reachability plus URL path alone to authorize termination of the event receiver. Options: (1) bind the event-listener strictly to loopback and treat any exposure beyond that as a deployment error/hard requirement; (2) require a shared secret/token (e.g., an HMAC or bearer token known only to the local node and signer, sent as a header) checked before honoring `/shutdown`, `/stackerdb_chunks`, etc.; (3) replace the HTTP-triggered shutdown mechanism entirely with a purely in-process signal (e.g., waking the blocking `http_server.recv()` via a self-pipe / OS-level shutdown of the listening socket) so no network path can trigger termination at all.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use std::net::{TcpListener, TcpStream};
use std::io::Write;

#[test]
fn unauthenticated_shutdown_dos() {
    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = "127.0.0.1:0".parse().unwrap();
    // bind() requires a fixed port in this codebase's HttpServer::http; use a free port helper
    // (omitted here for brevity) to get a concrete SocketAddr `bound`.
    let bound = receiver.bind(addr).unwrap();

    // Spawn the event receiver loop in a thread, as `Signer::spawn` does.
    let handle = std::thread::spawn(move || receiver.main_loop());

    // Attacker: independent TCP client, no ownership of a stop signaler.
    let mut stream = TcpStream::connect(bound).unwrap();
    let body = "attacker controlled";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // The event thread should exit shortly after processing the forged /shutdown.
    handle.join().unwrap();
    // Assertion: main_loop() returned (i.e., it broke out on Err(EventError::Terminated))
    // without `get_stop_signaler()` ever having been called by a legitimate owner.
}
```
This reproduces the exact byte sequence built by `SignerStopSignaler::send()` at [5](#0-4)  from an attacker-controlled `TcpStream`, and the assertion point is the `Err(EventError::Terminated) => break` arm in `main_loop()` at [6](#0-5) .

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

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L443-446)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
```
