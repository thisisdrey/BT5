## Analog Found

### Title
Signer's `SignerEventReceiver` main loop is blocked indefinitely by a single slow/stalled connection — ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer`'s HTTP event-receiver, which listens for block proposals, burn-block notifications, and StackerDB chunk events pushed by the local `stacks-node`, runs a single-threaded loop that performs a blocking `recv()` on a `tiny_http::Server` with no read/accept timeout configured. Any TCP client that connects to the signer's event port and withholds or trickles the HTTP request will stall this loop indefinitely, exactly mirroring the Keylime `registrar` bug where one open connection blocks the whole service.

### Finding Description
`SignerEventReceiver::bind()` creates a `tiny_http::Server` (aliased `HttpServer`) with no explicit timeout settings [1](#0-0) . The `EventReceiver::main_loop()` default implementation runs in a single dedicated thread and repeatedly calls `next_event()`, which calls `http_server.recv()` and blocks until a full HTTP request head is available [2](#0-1) [3](#0-2) .

The receiver is spawned in exactly one dedicated OS thread (`event_thread`), separate from the signer runloop thread, with no per-connection worker threads and no accept/read timeout applied to the underlying socket [4](#0-3) . Because `recv()` is the *sole* mechanism by which the loop discovers new work, a client that opens the TCP connection and never completes sending the HTTP request headers (or sends them at an arbitrarily slow rate) causes `recv()` to block forever on that one connection, starving all other legitimate producers (the local `stacks-node`, which pushes `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/status` events) from having their requests processed at all.

This is structurally identical to CVE-2023-38200: a single-threaded server loop whose only forward-progress path is a blocking `accept`/`recv()` call with no timeout, such that one unresponsive peer denies service to everyone else.

### Impact Explanation
If the signer's event-receiver endpoint is reachable by an unprivileged remote party (e.g., it is not strictly bound to loopback in the operator's deployment, or is reachable from another host on the same subnet as the node), a single stalled connection blocks the signer from receiving `BlockProposal` notifications, `NewBurnBlock` events, and `StackerDBChunksEvent`s from its own node indefinitely. This can cause the signer to miss block-signing windows, effectively denying its participation in consensus with only one non-data-bearing connection — matching the "Critical: remote crash/unauthenticated DoS from few messages" bar, since the attack requires zero completed messages, just one open socket.

### Likelihood Explanation
Likelihood depends on network exposure of the signer's event-receiver bind address; if operators bind it to a non-loopback interface (which the config allows, since `bind_addr`/`endpoint` is fully configurable per `Signer::spawn`) [5](#0-4) , this is trivially exploitable by any host that can reach that port. Because there's no code-level timeout enforcement, the vulnerability is present regardless of deployment as a latent risk; only network exposure gates exploitability.

### Recommendation
Configure a read/accept timeout on the `tiny_http::Server` (or switch to a non-blocking/poll-based accept model, as is already used elsewhere in `stackslib/src/net/server.rs` and `stackslib/src/net/poll.rs`), and/or handle each accepted connection on its own worker thread/task so that one stalled client cannot block the receipt of subsequent events. At minimum, apply `set_read_timeout`/`set_nonblocking` semantics analogous to what `stackslib/src/net/httpcore.rs` already does for outbound requests [6](#0-5) .

### Proof of Concept
1. Start a `stacks-signer` bound to a reachable interface (or on the loopback and connect locally to demonstrate the primitive).
2. From an unprivileged client, open a raw TCP connection to the signer's event port and send nothing (or send `"POST /status HTTP/1.1\r\n"` without ever completing the headers), then hold the connection open.
3. Have the real `stacks-node` (or a test harness mimicking `libsigner/src/tests/mod.rs`'s `test_simple_signer` mock node [7](#0-6) ) attempt to POST a legitimate `/stackerdb_chunks` or `/proposal_response` event to the same port.
4. Observe that `next_event()`'s call to `http_server.recv()` remains blocked on the attacker's stalled connection, and the legitimate event is never processed by `main_loop()` until the attacker's connection is closed or times out at the OS/TCP layer.

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

**File:** libsigner/src/events.rs (L413-421)
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
```

**File:** libsigner/src/runloop.rs (L209-225)
```rust
    pub fn spawn(&mut self, bind_addr: SocketAddr) -> Result<RunningSigner<EV, R, T>, EventError> {
        let mut event_receiver = self
            .event_receiver
            .take()
            .ok_or(EventError::AlreadyRunning)?;
        let result_sender = self
            .result_sender
            .take()
            .ok_or(EventError::AlreadyRunning)?;
        let mut signer_loop = self.signer_loop.take().ok_or(EventError::AlreadyRunning)?;

        let (event_send, event_recv) = channel();
        event_receiver.add_consumer(event_send);

        let bind_port = bind_addr.port();
        event_receiver.bind(bind_addr)?;
        let stop_signaler = event_receiver.get_stop_signaler()?;
```

**File:** libsigner/src/runloop.rs (L228-236)
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
```

**File:** stackslib/src/net/httpcore.rs (L1976-1979)
```rust

    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    stream.set_nodelay(true)?;
```

**File:** libsigner/src/tests/mod.rs (L121-146)
```rust
    let mock_stacks_node = thread::spawn(move || {
        let mut num_sent = 0;
        while num_sent < thread_chunks.len() {
            let mut sock = match TcpStream::connect(endpoint) {
                Ok(sock) => sock,
                Err(..) => {
                    sleep_ms(100);
                    continue;
                }
            };

            let ev = &thread_chunks[num_sent];
            let body = serde_json::to_string(ev).unwrap();
            let req = format!(
                "POST /stackerdb_chunks HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                endpoint,
                body.len(),
                body
            );
            debug!("Send:\n{}", &req);

            sock.write_all(req.as_bytes()).unwrap();
            sock.flush().unwrap();

            num_sent += 1;
        }
```
