### Title
Unauthenticated `/shutdown` endpoint terminates signer's `SignerEventReceiver` main loop - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver::next_event` HTTP handler treats any POST to `/shutdown` as a valid termination command with zero authentication, signature check, or secret comparison. Any TCP peer that can reach the signer's event-listener port can send a single raw HTTP POST and permanently stop the signer's event receiver loop.

### Finding Description
In `next_event`, request routing is done purely on `request.url()` and `request.method()` with no caller identity check: [1](#0-0) 

When `request.url() == "/shutdown"`, the handler immediately does `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returns `Err(EventError::Terminated)`, with no verification of any secret, signature, or source. This is exactly the mechanism `SignerStopSignaler::send()` uses internally to signal shutdown from the same process: [2](#0-1) 

`EventReceiver::main_loop` treats `EventError::Terminated` from `next_event()` as a clean exit condition, breaking out of the loop entirely: [3](#0-2) 

Unlike `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block`, which route through `process_event` and involve deserializing signed StackerDB chunk data or block/proposal payloads originating from the trusted local node, the `/shutdown` path has no payload validation at all — the body is not even read or checked; the mere URL match is sufficient. There is no comparison against an RPC secret, no peer-address allowlist, and no application-layer authentication anywhere in this dispatch path.

The event-receiver HTTP server is bound via `bind()` at signer startup, to the `endpoint` address configured for the signer to receive event pushes from the node: [4](#0-3) 

Any TCP client that can complete a connection to that bound socket (regardless of whether it is the actual Stacks node) can send `POST /shutdown HTTP/1.1\r\nHost: ...\r\nConnection: close\r\n\r\n` and have the same effect as the legitimate internal stop signal.

### Impact Explanation
A single unauthenticated HTTP request permanently sets `stop_signal` to `true`, causing `main_loop` to exit and the `event_join` thread to terminate. Since `next_event()` checks `is_stopped()` at the top of each iteration and returns `Err(EventError::Terminated)` once stopped, the signer's event receiver never processes another `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` push from the node again — it is a one-shot, unauthenticated, repeatable-per-restart denial of service against the signer's connectivity to the node event stream. This matches the Critical category: "remote crash/unauthenticated DoS from few messages" (in this case, exactly one message, no crash but a silent, permanent stop of the receive loop).

### Likelihood Explanation
No preconditions beyond TCP reachability to the signer event-listener port are required — no RPC secret, no peer key, no StackerDB slot ownership. The attacker cost is a single crafted HTTP POST request. It is trivially repeatable across signer restarts and requires no timing or race condition.

### Recommendation
Require an authentication check on the `/shutdown` route (and ideally all routes) — e.g., a locally generated shared secret/token known only to the node and signer process, or restrict the listening socket to loopback/local-only binding and reject non-local peers before dispatching to `next_event`. At minimum, the `/shutdown` command should only be honored on connections that originate from `127.0.0.1`/the local host, or better, use the existing inter-thread `Arc<AtomicBool>` mechanism exclusively in-process and remove the network-triggerable `/shutdown` HTTP path entirely, replacing it with a local Unix pipe/socket or OS signal-based approach.

### Proof of Concept
```rust
// libsigner/src/tests/mod.rs (new test)
#[test]
fn test_unauthenticated_shutdown_dos() {
    let mut ev: SignerEventReceiver<SignerMessage> = SignerEventReceiver::new(false);
    let endpoint: SocketAddr = "127.0.0.1:32000".parse().unwrap();
    ev.bind(endpoint).unwrap();

    let main_loop_thread = {
        let mut ev = ev; // move into thread
        thread::spawn(move || {
            ev.main_loop();
        })
    };

    // Attacker: unauthenticated client, no secret, no signature
    let mut sock = TcpStream::connect(endpoint).unwrap();
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {endpoint}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    );
    sock.write_all(req.as_bytes()).unwrap();
    sock.flush().unwrap();

    // main_loop should exit promptly because stop_signal was set unauthenticated
    main_loop_thread.join().unwrap();
    // Assertion: reaching here without hanging proves the main loop exited
    // due to the unauthenticated /shutdown request.
}
```
Expected result: `main_loop_thread.join()` returns quickly (loop exits), confirming that `event_receiver.stop_signal.store(true, Ordering::SeqCst)` at `libsigner/src/events.rs:444` was triggered by an unauthenticated client, matching the claimed fault.

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

**File:** libsigner/src/events.rs (L376-395)
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
```

**File:** libsigner/src/events.rs (L437-446)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
```

**File:** stacks-signer/src/lib.rs (L119-138)
```rust
impl<S: Signer<T> + Send + 'static, T: SignerEventTrait + 'static> SpawnedSigner<S, T> {
    /// Create a new spawned signer
    pub fn new(config: GlobalConfig) -> Self {
        let endpoint = config.endpoint;
        info!("Stacks signer version {:?}", VERSION_STRING.as_str());
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
        let (res_send, res_recv) = channel();
        let ev = SignerEventReceiver::new(config.network.is_mainnet());
        crate::monitoring::actions::start_serving_monitoring_metrics(config.clone()).ok();
        let runloop = RunLoop::new(config.clone());
        let mut signer: RunLoopSigner<S, T> = libsigner::Signer::new(runloop, ev, res_send);
        let running_signer = signer.spawn(endpoint).expect("Failed to spawn signer");
```
