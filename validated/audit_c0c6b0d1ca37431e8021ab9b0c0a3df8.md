### Title
Unauthenticated `POST /shutdown` permanently terminates the signer's event-receiver loop - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` handles any HTTP POST to `/shutdown` by unconditionally setting `stop_signal` to `true` and returning `EventError::Terminated`, with no check that the request originated from the signer's own `SignerStopSignaler`. Any TCP peer that can reach the signer's event-receiver bind address can send this bare request to permanently halt the signer's event processing.

### Finding Description
The intended invariant is that `stop_signal` (an `Arc<AtomicBool>` shared between `SignerEventReceiver` and `SignerStopSignaler`) is only set by `SignerStopSignaler::send`, which is meant to be called only by the signer process itself to shut down its own event thread [1](#0-0) . However, the HTTP handler in `next_event` treats the URL path `/shutdown` as equivalent to a legitimate stop signal, with no secret, token, source-IP check, or any other authentication:

```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [2](#0-1) 

This branch is reached for any POST request whose method is `Post` (checked generically for all routes) and URL equals `/shutdown` [3](#0-2) . Once `stop_signal` is `true`, `is_stopped()` returns `true` on the next check [4](#0-3) , and `main_loop` breaks out on its next iteration, permanently ending event processing for that signer instance [5](#0-4) . There is no code path that restarts or resets `stop_signal` back to `false`, so this is a one-shot, irreversible kill switch reachable via one unauthenticated HTTP request.

### Impact Explanation
A single crafted `POST /shutdown HTTP/1.1` request from any party able to open a TCP connection to the signer's event-receiver listener permanently stops that signer instance from processing further StackerDB chunks, burn blocks, new blocks, and block-validation responses — effectively taking the signer offline until manually restarted. This matches the Critical category of "remote crash/unauthenticated DoS from few messages" since it requires exactly one unauthenticated message and causes a durable denial of service on a security-critical component (a Stacks signer whose availability is required for block signing).

### Likelihood Explanation
The only precondition is TCP reachability to the event-receiver's bound socket address, which is the same endpoint the local stacks-node's event dispatcher posts to; if it is bound to a non-loopback interface (which is a deployment configuration choice, not something the code prevents), it is remotely reachable. No secret, credential, StackerDB slot ownership, or any privileged role is needed — this is directly reachable by an unprivileged attacker as defined in scope. The attack costs a single HTTP request and is deterministic and repeatable against any newly restarted instance.

### Recommendation
Require the `/shutdown` route to authenticate the caller — e.g., verify a shared secret/token known only to the local node/signer pairing (similar to how other privileged control endpoints are protected), restrict the listener to loopback-only when used for this internal shutdown mechanism, or remove the HTTP-triggered shutdown path entirely and rely solely on in-process signaling (e.g., a separate local IPC mechanism) rather than an externally reachable, unauthenticated POST route.

### Proof of Concept
1. In a test module under `libsigner`, construct a `SignerEventReceiver::<T>` and call `bind()` on `127.0.0.1:0` to obtain the bound address.
2. Spawn `main_loop` (or directly call `next_event()` in a loop) on a background thread.
3. From a plain `TcpStream::connect` (not via `SignerStopSignaler`), write the raw bytes:
   `POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n`
4. Assert that `receiver.is_stopped()` becomes `true` shortly after, and that a subsequent call to `next_event()` returns `Err(EventError::Terminated)` — demonstrating termination triggered by an unauthenticated client rather than through `SignerStopSignaler::send`.

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

**File:** libsigner/src/events.rs (L430-445)
```rust
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
```

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
