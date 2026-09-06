### Title
Unbounded blocking body read in `SignerEventReceiver::next_event` allows single-connection DoS of signer event thread - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` calls `http_server.recv()` (tiny_http) and then `process_event` reads the full HTTP body via `request.as_reader().read_to_string(&mut body)`, with no read/write timeout configured anywhere on the underlying `TcpStream` or `HttpServer`. A remote peer that opens a TCP connection to the signer's event endpoint, sends a `POST /stackerdb_chunks` with a large declared `Content-Length` but only a partial body, and then stalls, causes the read to block indefinitely.

### Finding Description
`SignerEventReceiver::bind` constructs the server with `HttpServer::http(listener)` and never calls any timeout-setting API on the accepted socket [1](#0-0) . `next_event` dispatches synchronously to `process_event::<T, StackerDBChunksEvent>(request)` for `/stackerdb_chunks` [2](#0-1) , and `process_event` performs a blocking `request.as_reader().read_to_string(&mut body)` that only returns once it has consumed exactly `Content-Length` bytes (or the stream errors/closes) [3](#0-2) . Because `main_loop` invokes `self.next_event()` synchronously in a single loop iteration and only calls `forward_event` after `next_event` returns [4](#0-3) , this is the signer's only event-processing thread: while it is blocked reading a stalled body, it cannot `accept()` new connections nor process any other event (miner block proposals, other signers' StackerDB chunk notifications, burn-block events, etc.). A repo-wide search found no `set_read_timeout`/`with_timeout` call associated with `libsigner`'s HTTP server or `tiny_http::Request` handling, confirming there is no deadline on this read.

The attacker's message: connect a raw TCP socket to the signer event listener, send valid HTTP headers for `POST /stackerdb_chunks` with `Content-Length: <large N>`, write M<N bytes of body, then simply stop sending and keep the socket open (no need to ever close or complete it).

Existing guards that do NOT stop this: `ack_dispatcher`/JSON deserialization happen only after the body is fully read, so they never execute; there is no `MAX_MESSAGE_LEN`/body-size cap enforced before or during the blocking read; there is no authentication on this endpoint since it is meant to receive local event-observer callbacks, but nothing restricts remote TCP reachability at the code level in `bind`.

### Impact Explanation
A single crafted, partially-sent HTTP request permanently (until connection eventually times out at the OS level or is dropped by the attacker) stalls the signer's sole event-receiver thread, blocking delivery of all subsequent legitimate StackerDB chunk events, miner block proposals, and burn-block notifications to the signer runloop. This is a single-message, unauthenticated remote DoS of the signer's message-processing pipeline, matching the "Critical - remote crash/unauthenticated DoS from few messages" category, assuming the event endpoint is reachable from the attacker's network position.

### Likelihood Explanation
Preconditions: the event endpoint (bound via `SignerEventReceiver::bind` at the `endpoint` address configured in `stacks-signer/src/config.rs`) must be reachable by the attacker. This is a configuration-dependent precondition — if operators bind the signer's event-observer endpoint strictly to `127.0.0.1` and firewall it, the attack is not remotely reachable; the question's stated precondition ("attacker has raw TCP access to the signer's event port") assumes it is. The `endpoint` is a configurable listen address (not hardcoded to loopback) [5](#0-4) , so deployments that bind to a non-loopback interface (or expose it via port-forwarding/misconfiguration) are exposed. Given reachability, the attack requires only one partially-sent HTTP request and zero authentication, and is trivially repeatable (each new connection consumes the single-threaded receiver again once the first one is eventually released, or can be layered with multiple stalled connections since `tiny_http`'s single-threaded `recv()`+read loop only ever processes one at a time).

### Recommendation
Set an explicit read (and ideally write) timeout on the accepted TCP stream / `HttpServer` request reader in `SignerEventReceiver::bind` or before reading the body in `process_event` (e.g., via `tiny_http::Server::http` + wrapping the incoming stream to call `set_read_timeout`, or migrating to a server abstraction that supports per-connection timeouts). Additionally, enforce a maximum allowed `Content-Length`/body size before beginning the read, and reject/drop connections that don't complete within the timeout window, returning an error from `next_event` rather than blocking indefinitely.

### Proof of Concept
Rust test outline (net test, not included in this codebase yet):
```rust
// 1. Construct a SignerEventReceiver<T>, call bind() on 127.0.0.1:<port>, spawn main_loop() in a thread.
// 2. Connect a raw TcpStream to the bound address.
// 3. Write: "POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\n" + only 10 bytes of body.
// 4. Do NOT close the stream; sleep.
// 5. From a second TcpStream, send a well-formed "/status" or legitimate "/stackerdb_chunks" request.
// 6. Assert: the second request never receives a response / the out_channel never receives the corresponding SignerEvent within a bounded timeout (e.g. 5s),
//    proving next_event() is permanently blocked inside process_event's read_to_string call at
//    libsigner/src/events.rs:526.
```

### Citations

**File:** libsigner/src/events.rs (L284-310)
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
```

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L437-438)
```rust
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
```

**File:** libsigner/src/events.rs (L524-533)
```rust
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
```

**File:** stacks-signer/src/config.rs (L1-1)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
```
