### Title
Unauthenticated remote panic in `SignerEventReceiver`'s `/status` handler crashes the signer's event-receiver thread - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event()` in `libsigner/src/events.rs` responds to the `/status` health-check endpoint by calling `.expect("response failed")` on the result of writing the HTTP response. Every other response path in this file (`ack_dispatcher`, the general error paths) gracefully logs and continues on I/O failure, but the `/status` path does not. A remote, unauthenticated peer that opens a TCP connection to the signer's event-receiver port, sends a `GET /status` request, and then aborts the connection before the response is fully written can trigger a write error that this `.expect()` turns into a panic, permanently killing the dedicated event-receiver thread and stopping the signer from processing any further node events — closely mirroring the reported Vault issue where a single unauthorized/malformed connection caused the whole listener to stop responding.

### Finding Description
`SignerEventReceiver` is the HTTP server signers run to receive events (StackerDB chunks, block proposals, burn blocks) pushed by the Stacks node [1](#0-0) . Its `next_event()` implementation dispatches based on URL, and for the `/status` health-check it writes the response and unconditionally unwraps the result: [2](#0-1) 

Contrast this with the general dispatcher-ack helper used elsewhere in the same file, which treats a failed response write as a recoverable, logged error rather than a panic: [3](#0-2) 

`next_event()` is called from `main_loop()`, which runs continuously on a dedicated OS thread spawned by `Signer::spawn()`: [4](#0-3) [5](#0-4) 

If a TCP client connects, sends `GET /status HTTP/1.1`, and then closes/resets the connection before (or while) the server attempts to write its `HTTP/1.1 200 OK` response, the underlying socket write inside `request.respond(...)` returns an `Err`. Because that call site uses `.expect("response failed")` instead of graceful error handling, the panic unwinds through `next_event()` and `main_loop()`, terminating the event-receiver thread. Unlike the rest of the network-input-handling code in this repo — which per project convention (see `CONTRIBUTING.md`: "Untrusted data ingestion must not panic") — this handler breaks that invariant for a trivially-reachable, unauthenticated code path.

Once the thread dies, the `SignerEventReceiver` no longer accepts new connections nor forwards events to the signer runloop; nothing in `Signer`/`RunningSigner` automatically restarts it. This is the same fault class as the reported Vault advisory: an unauthenticated network peer causes an otherwise-healthy listener/server component to stop servicing all further requests, requiring an operator to notice and restart the process.

### Impact Explanation
This is a remote, unauthenticated crash reachable with a single crafted TCP interaction (connect, send `GET /status`, abort). It permanently disables the signer's event ingestion path (StackerDB chunk events, block-validation responses, burn-block notifications) until the operator restarts the signer process, which can stall signing operations for that signer. This matches the "Critical - remote crash/unauthenticated DoS from few messages" impact bucket.

### Likelihood Explanation
The `/status` endpoint takes no authentication and requires no valid payload — a bare GET request suffices, and any TCP peer that can reach the signer's listening address can trigger it. Reliably forcing the write to fail (versus succeeding before disconnect) may require some timing/race (e.g., an immediate RST after connect, or exhausting the client's receive buffer), but a very short "OK" response combined with abrupt socket close/RST is a well-known low-effort technique for forcing write failures on the server side, making this a realistic and repeatable attack even if not 100% deterministic on the first attempt.

### Recommendation
Change the `/status` handler to not panic on a failed response write; log the error and return an appropriate `Result`/continue the loop, consistent with `ack_dispatcher` and the rest of the file's error-handling pattern:
```rust
if request.url() == "/status" {
    if let Err(e) = request.respond(HttpResponse::from_string("OK")) {
        warn!("Failed to respond to /status check: {:?}", e);
    }
    return Ok(SignerEvent::StatusCheck);
}
```
More generally, audit `libsigner/src/events.rs` for any other `.expect()`/`.unwrap()` calls on socket I/O reachable from untrusted network input, and consider wrapping `main_loop()`'s body in a panic-catching mechanism (or auto-respawn logic) so that a single malformed/aborted connection cannot permanently disable the event receiver.

### Proof of Concept
1. Start a signer with `SignerEventReceiver` bound to `ip:port`.
2. From an unauthenticated remote host, open a raw TCP socket to `ip:port`.
3. Send `GET /status HTTP/1.1\r\nHost: ip:port\r\nConnection: close\r\n\r\n`.
4. Immediately close/reset the socket (e.g., set `SO_LINGER` to force RST, or simply drop the socket without reading the response) before the server can flush its response bytes.
5. Repeat if necessary to win the race between the server's `respond()` write and the client's abrupt close.
6. Observe (via signer logs / thread panic) that the event-receiver thread panics and the signer subsequently stops receiving any StackerDB/block/burn-block events — corresponding to lines 424–427 of `libsigner/src/events.rs`.

### Citations

**File:** libsigner/src/events.rs (L282-312)
```rust
    /// Main loop for the receiver.
    /// Typically, this is started in a separate thread.
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

**File:** libsigner/src/events.rs (L315-327)
```rust
/// Event receiver for Signer events
pub struct SignerEventReceiver<T: SignerEventTrait> {
    /// Address we bind to
    local_addr: Option<SocketAddr>,
    /// server socket that listens for HTTP POSTs from the node
    http_server: Option<HttpServer>,
    /// channel into which to write newly-discovered data
    out_channels: Vec<Sender<SignerEvent<T>>>,
    /// inter-thread stop variable -- if set to true, then the `main_loop` will exit
    stop_signal: Arc<AtomicBool>,
    /// Whether the receiver is running on mainnet
    is_mainnet: bool,
}
```

**File:** libsigner/src/events.rs (L413-428)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

            if request.url() == "/status" {
                request
                .respond(HttpResponse::from_string("OK"))
                .expect("response failed");
                return Ok(SignerEvent::StatusCheck);
            }
```

**File:** libsigner/src/events.rs (L511-515)
```rust
fn ack_dispatcher(request: HttpRequest) {
    if let Err(e) = request.respond(HttpResponse::empty(200u16)) {
        error!("Failed to respond to request: {:?}", &e);
    };
}
```

**File:** libsigner/src/runloop.rs (L228-247)
```rust
        // start a thread for the event receiver
        let event_thread = thread::Builder::new()
            .name(format!("event_receiver:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || event_receiver.main_loop())
            .map_err(|e| {
                error!("EventReceiver failed to start: {:?}", &e);
                EventError::FailedToStart
            })?;

        // start receiving events and doing stuff with them
        let runloop_thread = thread::Builder::new()
            .name(format!("signer_runloop:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || signer_loop.main_loop(event_recv, result_sender, stop_signaler))
            .map_err(|e| {
                error!("SignerRunLoop failed to start: {:?}", &e);
                ret_stop_signaler.send();
                EventError::FailedToStart
            })?;
```
