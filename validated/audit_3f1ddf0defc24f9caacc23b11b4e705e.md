### Title
Unauthenticated `/shutdown` request terminates signer event receiver - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` matches any HTTP POST to `/shutdown` and unconditionally sets the stop signal and returns `EventError::Terminated`, with no check that the request originated from the co-located `SignerStopSignaler`. Any TCP client that can reach the signer's event-listener socket can send a single crafted HTTP request to permanently halt the signer's event loop.

### Finding Description
The intended equality is "the stop signal is set" == "the local signer operator (via `SignerStopSignaler::send`) requested shutdown," since `SignerStopSignaler::send` is the only sanctioned way to set `stop_signal`: it stores `true` in the shared `AtomicBool` and then sends a `POST /shutdown` to the local address purely to wake up the blocking HTTP accept loop [1](#0-0) . However, `next_event` independently re-implements the same effect from the wire without checking the caller's identity: it dispatches on `request.url() == "/shutdown"`, stores `true` into `event_receiver.stop_signal`, and returns `Err(EventError::Terminated)` [2](#0-1) . There is no secret, token, source-IP check, or any other authentication gate before this branch — the only prior gate is that the method must be `POST` [3](#0-2) . `EventReceiver::main_loop` treats `EventError::Terminated` as a clean break condition, ending the thread that services the node's inbound events (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`) [4](#0-3) . Once that thread exits, the signer's `main_loop` (`SignerRunLoop`) only receives on a channel with a timeout and never restarts the event receiver, so the signer becomes permanently deaf to node events while its runloop thread keeps spinning uselessly [5](#0-4) . The equality is broken: any unauthenticated remote client, not just the local stop signaler, can flip the stop signal.

### Impact Explanation
A single unauthenticated HTTP POST to the signer's event socket permanently kills the event-receiving thread of a stacks-signer process, silently taking that signer offline from all future block-validation responses, StackerDB chunk events, and burn/Stacks block notifications until the operator manually restarts the process. This is a remote, unauthenticated, single-message DoS against a signer node — matching the Critical category ("remote crash/unauthenticated DoS from few messages"). It is trivially repeatable against every signer whose event endpoint is reachable.

### Likelihood Explanation
The only precondition is TCP reachability to the signer's bound event-listener address/port (the `endpoint` configured for the signer, which is what the stacks node is instructed to POST events to). No secret, key, StackerDB slot, or any privileged role is required — the attacker only needs to open a TCP connection and write a well-formed HTTP request; attacker cost is a single short-lived connection. This matches the allowed unprivileged-attacker capability of "any remote party who can connect to ... send arbitrary bytes."

### Recommendation
Do not honor a shutdown request originating from an arbitrary remote peer. Options: (1) restrict the event listener to bind only to loopback/a trusted management interface documented as the intended deployment (and enforce/verify this in code, e.g., rejecting connections not from `127.0.0.1`/configured trusted source); (2) require a shared secret/token in the `/shutdown` request that only `SignerStopSignaler` knows, validated before storing the stop signal; (3) separate the "wake up the accept loop" mechanism from the actual termination decision — e.g., use a dedicated local-only IPC/pipe or an internal flag set only by `SignerStopSignaler` itself (in-process, e.g. via the already-shared `Arc<AtomicBool>` handle) rather than trusting an HTTP path match to imply authorization.

### Proof of Concept
```rust
// libsigner: reproduces unauthenticated shutdown
use std::io::Write;
use std::net::TcpStream;
use std::thread;

let mut receiver: SignerEventReceiver<StacksBlockEvent> = SignerEventReceiver::new(false);
let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

let handle = thread::spawn(move || {
    receiver.main_loop(); // runs until Terminated
});

// Attacker: no SignerStopSignaler used, arbitrary external stream
let mut stream = TcpStream::connect(addr).unwrap();
let body = "attacker";
let req = format!(
    "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
    body.len(), body
);
stream.write_all(req.as_bytes()).unwrap();

handle.join().unwrap(); // succeeds -> main_loop exited from unauthenticated request
```
The assertion is that `main_loop` returns (thread joins) purely from the crafted external request, without ever invoking `SignerStopSignaler::send`, confirming `is_stopped()` becomes `true` and `EventReceiver::main_loop` breaks at the `EventError::Terminated` branch [6](#0-5) .

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

**File:** libsigner/src/events.rs (L430-436)
```rust
            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
            debug!("Processing {} event", request.url());
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** libsigner/src/runloop.rs (L59-82)
```rust
    fn main_loop<EVST: EventStopSignaler>(
        &mut self,
        event_recv: Receiver<SignerEvent<T>>,
        result_send: Sender<R>,
        mut event_stop_signaler: EVST,
    ) -> Option<R> {
        info!("Signer runloop begin");
        loop {
            let poll_timeout = self.get_event_timeout();
            let next_event_opt = match event_recv.recv_timeout(poll_timeout) {
                Ok(event) => Some(event),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => {
                    info!("Event receiver disconnected");
                    return None;
                }
            };
            if let Some(final_state) = self.run_one_pass(next_event_opt, &result_send) {
                info!("Runloop exit; signaling event-receiver to stop");
                event_stop_signaler.send();
                return Some(final_state);
            }
        }
    }
```
