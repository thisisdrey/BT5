### Title
Unauthenticated `POST /shutdown` permanently terminates the signer's event loop - (File: libsigner/src/events.rs)

### Summary
The `SignerEventReceiver::next_event` handler treats any HTTP request with `url() == "/shutdown"` and `method() == POST` as a legitimate stop command, setting `stop_signal` to `true` via `Ordering::SeqCst` with zero authentication of the sender. Any TCP client that can reach the bound event port can send this single request and permanently kill the signer's event-consumption loop.

### Finding Description
The event endpoint is designed so that the node pushes events (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) to the signer over a locally-bound HTTP listener created by `SignerEventReceiver::bind` [1](#0-0) . The intended shutdown mechanism is `SignerStopSignaler::send`, which opens its own `TcpStream` to `local_addr` and sends a raw, unauthenticated `POST /shutdown` request purely to wake up the blocking `http_server.recv()` call so the already-set `AtomicBool` is observed [2](#0-1) .

However, the actual state mutation happens inside `next_event`, which matches on the URL/method of *any* incoming request, not on any token, source, or the internal `stop_signal` state:
```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [3](#0-2) 

There is no check of caller identity, no shared secret, no signature, and no requirement that the request actually originated from `SignerStopSignaler` — the branch itself is the sole authority that sets `stop_signal`. Any TCP peer able to open a connection to this port and send `POST /shutdown HTTP/1.1\r\nContent-Length: 0\r\n\r\n` triggers the same store and returns `EventError::Terminated`, which `main_loop` treats as an intentional exit: `Err(EventError::Terminated) => { info!("Caught termination signal"); break; }` [4](#0-3) . Once `main_loop` returns, the signer stops consuming StackerDB chunks, burn-block events, and block-validation responses, breaking the availability invariant that "the signer's event pipeline stays live" == "the signer intends to keep participating." None of the existing guards (`MAX_MESSAGE_LEN`, StackerDB chunk-signature checks, `will_admit_mempool_tx`, canonical-tip resolution) apply here because this is a distinct, unauthenticated control-plane endpoint that bypasses all of them.

### Impact Explanation
A single unauthenticated TCP message permanently and irrecoverably stops a signer's event ingestion thread (`is_stopped()` becomes true forever; the `AtomicBool` is never reset). This is a remote, unauthenticated denial-of-service against the signer process's ability to receive miner block proposals, StackerDB signer messages, and burn/stacks block notifications, effectively removing that signer from consensus participation with a single crafted HTTP request and no credentials. This matches the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs network reachability to the bound event-receiver socket (the same reachability the node-signer local channel uses) and the ability to send raw bytes; no RPC secret, slot ownership, or peer key is required. The attack is a single, cheap, repeatable HTTP request that requires no prior state, no valid StackerDB chunk signature, and no knowledge of any secret. If the event listener is bound to an interface reachable beyond localhost (e.g., `0.0.0.0` in a given deployment/config), the attack is trivially remote; even if commonly bound to loopback in default configurations, the code contains no authentication whatsoever, so any process capable of reaching that socket (including from a different container, VM, or misconfigured binding) can execute this DoS.

### Recommendation
Do not let the wire-visible URL/method alone authorize a state-changing shutdown. Options:
- Bind the event listener strictly to a loopback-only address and additionally require a shared, randomly-generated per-process secret/token (e.g., a header or path segment known only to `SignerStopSignaler`) before honoring `/shutdown`.
- Alternatively, remove the network-triggered shutdown mechanism entirely and replace it with a purely in-process mechanism to wake `http_server.recv()` (e.g., use `tiny_http`'s built-in ability to interrupt via closing the listening socket, or a self-pipe/local unix socket not reachable externally) so that no externally reachable HTTP request can influence `stop_signal`.
- At minimum, validate that the `/shutdown` request originates from `127.0.0.1`/the local machine before honoring it, in addition to a shared secret, since IP-based checks alone are insufficient if the port is exposed via a shared network namespace.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a new integration test
use std::io::Write;
use std::net::TcpStream;
use std::sync::mpsc::channel;
use std::thread;

#[test]
fn unauthenticated_shutdown_terminates_event_receiver() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> =
        SignerEventReceiver::new(false);
    let addr = "127.0.0.1:0".parse().unwrap();
    let bound_addr = receiver.bind(addr).unwrap();
    let (tx, _rx) = channel();
    receiver.add_consumer(tx);

    // Attacker: raw TCP client with no credentials, no stop signaler object.
    let handle = thread::spawn(move || {
        let mut stream = TcpStream::connect(bound_addr).unwrap();
        let req = "POST /shutdown HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n";
        stream.write_all(req.as_bytes()).unwrap();
    });

    // Before: not stopped.
    assert!(!receiver.is_stopped());

    // next_event should observe the forged /shutdown and set stop_signal.
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped()); // stop_signal permanently true

    // Subsequent calls always terminate, without any authenticated stop signaler.
    assert!(matches!(receiver.next_event(), Err(EventError::Terminated)));

    handle.join().unwrap();
}
```
Assertion/crash site: `event_receiver.stop_signal.store(true, Ordering::SeqCst)` in `libsigner/src/events.rs:444`, reachable purely from wire bytes without going through `SignerStopSignaler`.

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
