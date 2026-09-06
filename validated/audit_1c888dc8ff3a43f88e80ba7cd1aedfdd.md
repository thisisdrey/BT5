## Analysis

The `SignerEventReceiver` runs a single-threaded `tiny_http::Server` whose `main_loop` calls `next_event()` → `http_server.recv()` → `process_event::<T,E>(request)` synchronously, all on one thread, with no worker pool. [1](#0-0) [2](#0-1) 

Inside `process_event`, the body is read via `request.as_reader().read_to_string(&mut body)` with no explicit size cap, no `MAX_MESSAGE_LEN` enforcement, and no socket read-timeout configured anywhere in `bind()` or on the underlying `HttpServer`/`TcpListener`. [3](#0-2) [4](#0-3) 

`tiny_http`'s request reader is bounded by the declared `Content-Length` header — it will keep waiting to read up to that many bytes from the TCP socket and only returns when either that many bytes have been read or the connection errors/closes. If a client declares a huge `Content-Length` (e.g. `999999999`) but only sends a short body and then simply stalls (never closing, never sending more data, no keep-alive timeout configured), `read_to_string` blocks indefinitely waiting for more bytes on that one connection. Since `recv()`/`process_event` execute synchronously on the sole processing thread inside `next_event()`, this blocks the entire event-receiver loop — no other request (legitimate node-forwarded `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`) can be accepted or processed until the slow read finishes or the socket errors out.

This matches the question's claim: `read_to_string` here is an **unbounded wait against an attacker-declared `Content-Length`**, not a bounded read against a validated maximum — there is no cap comparable to `MAX_HTTP_HEADER_LEN`/`MAX_HTTP_HEADERS` (used only for header parsing in `libsigner/src/http.rs`, a separate/unused-by-this-path decoder) or `MAX_MESSAGE_LEN` applied to this body read. [5](#0-4) 

One caveat on reachability: this endpoint is the signer's local event-listener socket that the **node's event-observer feature** posts to (configured via the signer's `endpoint`/the node's `event_observer` config), not the node's public P2P or RPC port. Whether it is remotely reachable by an arbitrary unprivileged internet attacker depends entirely on the operator's deployment (bind address/firewalling) — the code itself does not restrict binding to loopback, and `bind()` accepts whatever `SocketAddr` is configured. [4](#0-3)  I could not verify the default/typical bind address from `stacks-signer/src/config.rs` in this pass, so whether this socket is normally exposed beyond localhost/private network remains unconfirmed from the code alone.

### Title
Unbounded HTTP body read in `process_event` allows single-connection DoS of the signer's event pipeline - (File: `libsigner/src/events.rs`)

### Summary
`process_event` reads the HTTP request body via `request.as_reader().read_to_string(&mut body)` with no length cap and no socket read-timeout, on the single thread that also calls `HttpServer::recv()` in `next_event()`. An attacker who can open a TCP connection to the signer's event-listener socket can send headers declaring a large `Content-Length` and then stall, blocking body reading indefinitely and starving all subsequent `next_event()` calls, including legitimate node-forwarded events.

### Finding Description
`next_event()` synchronously calls `http_server.recv()` and then dispatches to `process_event::<T,E>(request)` on the same thread [2](#0-1) . `process_event` reads the entire body with `read_to_string` before doing any deserialization or length validation [6](#0-5) . `tiny_http`'s body reader for a `Content-Length`-declared request will not return until it has read that many bytes (or the connection errors/EOFs). No `set_read_timeout` is applied to the listening socket or accepted connections in `bind()` [4](#0-3) , and no maximum body size is enforced before or during the read (unlike header parsing in `libsigner/src/http.rs`, which is a separate, unused-for-this-path code path with `MAX_HTTP_HEADER_LEN`). An attacker opens a raw TCP connection to the bound address, sends `POST /stackerdb_chunks HTTP/1.1\r\nContent-Length: 999999999\r\n\r\n` plus a short body, and never sends the rest nor closes the socket. `read_to_string` blocks on that connection forever (or until an OS-level TCP timeout, which can be very long). Because `main_loop`/`next_event` process one connection at a time on a single thread, this stalls the entire loop, preventing the node's legitimately forwarded events from ever being dequeued via `recv()`.

### Impact Explanation
This is a bounded-compute/single-connection denial of service against the signer's own event pipeline: one crafted, unauthenticated connection can stall all downstream processing of node-forwarded StackerDB chunks, burn-block, and block-validation-response events, effectively taking the signer offline for signing duties while the connection is held open.

### Likelihood Explanation
No authentication or secret is required to connect to this endpoint and no valid payload is needed — only a raw TCP connection and a crafted header. The primary uncertainty is deployment-dependent reachability: this socket is intended to receive the node's event-observer POSTs, and whether it is bound to a loopback/private address or an address reachable by an arbitrary remote attacker depends on signer configuration, which was not fully confirmed in this pass.

### Recommendation
Set an explicit read/write timeout on accepted connections (or the underlying listener) in `bind()`, and enforce a maximum body length before/while reading in `process_event` (e.g., check `Content-Length` against a cap such as `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN` and use a bounded reader like `Read::take(cap)` instead of unbounded `read_to_string`). Consider moving request handling off the accept thread so one slow/malicious connection cannot starve the receive loop.

### Proof of Concept
1. Start a `SignerEventReceiver` and call `bind()` to obtain its address; spawn `next_event()` in a loop on a background thread forwarding to a channel.
2. From a test, open a raw `TcpStream` to that address and write:
   `"POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\nshortbody"` without closing the socket or sending more bytes.
3. From a second `TcpStream`, send a legitimate, well-formed `/status` or `/stackerdb_chunks` request with a correct `Content-Length` matching the actual body.
4. Assert that the legitimate request's `next_event()`/response is not received within a short timeout (e.g. 5s), demonstrating that `next_event` is starved by the first, still-open connection — the assertion fails only if the fix (timeout/bounded read) is applied.

### Citations

**File:** libsigner/src/events.rs (L282-313)
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

**File:** libsigner/src/events.rs (L413-459)
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
            } else if request.url() == "/new_block" {
                process_event::<T, StacksBlockEvent>(request)
            } else {
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
    }
```

**File:** libsigner/src/events.rs (L519-533)
```rust
fn process_event<T, E>(mut request: HttpRequest) -> Result<SignerEvent<T>, EventError>
where
    T: SignerEventTrait,
    E: serde::de::DeserializeOwned + TryInto<SignerEvent<T>, Error = EventError>,
{
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

**File:** libsigner/src/http.rs (L27-28)
```rust
pub const MAX_HTTP_HEADERS: usize = 32;
pub const MAX_HTTP_HEADER_LEN: usize = 4096;
```
