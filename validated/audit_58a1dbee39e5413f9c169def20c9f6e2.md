### Title
Unauthenticated remote DoS via `POST /shutdown` on the signer event-receiver listener - (`libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` treats a bare HTTP `POST /shutdown` as a valid, unauthenticated command that permanently sets `stop_signal` and returns `EventError::Terminated`, which unconditionally breaks `main_loop`. There is no check that the request actually originated from `SignerStopSignaler::send` (i.e., from the local node process) rather than from any remote TCP peer that can reach the bound event-listener socket.

### Finding Description
The claimed equality — "the party that can set `stop_signal` == the configured node process" — is broken: `next_event` matches purely on `request.url() == "/shutdown"` [1](#0-0)  with no signature, shared secret, source-IP check, or any correlation with `SignerStopSignaler::send`'s crafted request. `SignerStopSignaler::send` merely opens a `TcpStream` and writes a fixed `POST /shutdown ...` HTTP request with no authenticating token [2](#0-1) ; any TCP client can reproduce this exact byte sequence (or any POST to `/shutdown` with any body/headers, since only the URL and method are checked) against the receiver's `HttpServer`. Once matched, `stop_signal.store(true, Ordering::SeqCst)` is executed and `Err(EventError::Terminated)` is returned [1](#0-0) , and `main_loop` treats `EventError::Terminated` as a terminal, `break`-triggering condition with no retry [3](#0-2) . `is_stopped()` reads the same `AtomicBool` and would also short-circuit `next_event` on any subsequent iteration [4](#0-3) , so the receiver is disabled for good — there is no un-set/reset path.

### Impact Explanation
Any remote party that can open a TCP connection to the signer's event-receiver bind address can permanently halt that signer's `main_loop`, stopping it from receiving `stackerdb_chunks`, `proposal_response`, `new_burn_block`, and `new_block` events for the rest of the process's lifetime (until manual restart). This is a single-message, unauthenticated, repeatable-per-signer DoS of that signer's protocol participation, matching the "remote crash/unauthenticated DoS from few messages" Critical category, scoped to a single signer's event pipeline (not a consensus-wide compromise).

### Likelihood Explanation
No special privileges, secrets, or peer/slot ownership are required — only the ability to connect to the TCP port the operator configured for the signer's `[endpoint]`/event-receiver binding and send one crafted HTTP POST. Cost is a single short-lived TCP connection and a few dozen bytes. I could not fully verify within the available context whether operators are expected/documented to bind this listener only to a loopback/private interface (e.g., `127.0.0.1`) by default or convention in `stacks-signer/src/config.rs`; if the deployed configuration only ever binds to a local/internal address reachable solely by the co-located node process, this reduces reachability to local-only rather than remote-unprivileged. This is an important precondition that determines whether "any remote attacker" in the strict problem framing (reaches the bound listener port over the network) actually applies, and it should be confirmed against the operator's actual bind address/firewalling before treating this as network-remote-exploitable in a given deployment.

### Recommendation
Do not accept unauthenticated shutdown commands from the transport. Options: (1) remove the HTTP `/shutdown` path entirely and drive `stop_signal` purely via the in-process `Arc<AtomicBool>` handle already returned by `get_stop_signaler`/`SignerStopSignaler` without going over the network; (2) if a network wakeup is required to unblock a blocking `recv()`, authenticate it (e.g., a per-process random token embedded in the shutdown path/body, checked before storing `stop_signal`, or bind exclusively to loopback and rely on OS-level access control) so only the local node/process holding the token can trigger it.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a standalone net test
use std::io::Write;
use std::net::TcpStream;
use std::sync::mpsc::channel;
use std::thread;

#[test]
fn attacker_can_shutdown_receiver_without_stopsignaler() {
    let mut receiver: SignerEventReceiver<SomeSignerEventTraitImpl> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let (tx, _rx) = channel();
    receiver.add_consumer(tx);

    // Attacker: unrelated socket, NOT via SignerStopSignaler::send
    let mut attacker = TcpStream::connect(addr).unwrap();
    let body = "attacker payload";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(), body
    );
    attacker.write_all(req.as_bytes()).unwrap();

    // Assert: next_event() observes the forged shutdown and terminates
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());

    // main_loop, if run, exits immediately from here on
}
```
This demonstrates that an attacker connecting directly (bypassing `SignerStopSignaler::send`) can trigger the same `stop_signal` write at `libsigner/src/events.rs:444` and force `EventError::Terminated`, permanently ending the receiver's `main_loop`.

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

**File:** libsigner/src/events.rs (L416-418)
```rust
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
