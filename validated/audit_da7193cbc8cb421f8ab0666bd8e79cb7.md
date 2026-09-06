### Title
Unauthenticated `/shutdown` HTTP endpoint terminates signer event loop with a single POST - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` treats any HTTP `POST /shutdown` request received on the signer's bound event-receiver socket as authoritative, immediately setting the stop flag and terminating the event `main_loop`. There is no authentication, token, or peer-identity check distinguishing the legitimate `SignerStopSignaler::send` caller (the signer's own process) from any other TCP client that can reach the listening port.

### Finding Description
In `libsigner/src/events.rs`, `SignerEventReceiver::next_event` dispatches on `request.url()`: [1](#0-0) 

```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
```

This branch is reached for any HTTP POST whose path is `/shutdown` — there is no verification of a secret, token, source IP/loopback restriction, or any credential. Compare this to the intended caller, `SignerStopSignaler::send`, which simply opens a plain `TcpStream` to the receiver's bound address and writes a raw HTTP POST request with no authentication material: [2](#0-1) 

Any TCP client capable of constructing the same bytes (`POST /shutdown HTTP/1.1\r\nHost: ...\r\nContent-Length: N\r\n\r\n<body>`) and sending them to the same socket triggers the identical code path. Once `stop_signal` is set, `is_stopped()` returns true and `EventError::Terminated` propagates up into `EventReceiver::main_loop`: [3](#0-2) 

causing the loop to `break`, ending the entire event-receiver thread that feeds `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, and `StacksBlockEvent` into the signer's downstream channels — i.e., the signer stops receiving block proposals, burn blocks, and StackerDB chunk events until restarted.

### Impact Explanation
Any party able to open a TCP connection to the signer's bound event-receiver port (the port the node's event dispatcher posts events to) can send one crafted HTTP request and permanently stop the signer's event ingestion loop, with no retry/recovery logic shown in this code path (the loop simply exits; nothing re-arms `stop_signal` or restarts `main_loop`). This is a single-message, unauthenticated denial of service against a core signer subsystem — matching the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The only precondition is network reachability to the socket that `SignerEventReceiver::bind` listens on. No secret, slot ownership, peer key, or admin role is required — the handler performs zero authentication of the `/shutdown` request. Cost to the attacker is a single raw HTTP POST; the attack is trivially repeatable to keep the signer down after any restart attempt.

Note: I was not able to fully confirm, within the available context, what network interface/address the signer's event-receiver socket is configured to bind to by default (e.g., loopback-only vs. a publicly reachable address) — this determines whether the attacker needs mere LAN/same-host reachability or full internet reachability. Configuration for this bind address lives in `stacks-signer/src/config.rs` and is passed to `SignerEventReceiver::bind`, but I could not verify the default value or whether operators are expected/instructed to restrict it via firewall. Regardless of default binding, the code itself contains no application-layer authentication for `/shutdown`, so any entity that can reach that address/port (whether that's localhost-only, LAN, or public depending on deployment) can trigger the DoS.

### Recommendation
Add an authentication check before honoring `/shutdown` (and ideally other trusted-only endpoints):
- Generate a random per-process shared secret at `SignerEventReceiver` construction, pass it into `SignerStopSignaler`, and require it as a header/body token that `next_event` validates via constant-time comparison before setting `stop_signal`.
- Alternatively/additionally, restrict `bind()` to loopback (`127.0.0.1`/`::1`) only and reject non-loopback bind targets for the event receiver, and/or verify `request.remote_addr()` is loopback before honoring `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use std::net::{TcpStream, TcpListener};
use std::io::Write;
use std::thread;

#[test]
fn test_unauthenticated_shutdown() {
    let mut receiver: SignerEventReceiver<SomeTestMsg> = SignerEventReceiver::new(false);
    let addr = "127.0.0.1:0".parse().unwrap();
    let bound = receiver.bind(addr).unwrap();

    let handle = thread::spawn(move || {
        receiver.main_loop(); // should block until /shutdown
        receiver.is_stopped()
    });

    // Attacker: arbitrary TCP client, no credentials
    let mut stream = TcpStream::connect(bound).unwrap();
    let req = "POST /shutdown HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n";
    stream.write_all(req.as_bytes()).unwrap();

    let stopped = handle.join().unwrap();
    assert!(stopped, "main_loop terminated from unauthenticated remote POST /shutdown");
}
```
This reproduces the exact request `SignerStopSignaler::send` issues, but from an unrelated `TcpStream` with no secret — demonstrating that `next_event`'s `/shutdown` branch cannot distinguish the legitimate internal caller from an arbitrary remote sender.

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
