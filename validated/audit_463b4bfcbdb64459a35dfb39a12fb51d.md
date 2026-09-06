### Title
Unauthenticated event-receiver HTTP handler can be stalled indefinitely by an incomplete request body, blocking all signer event processing - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver` uses `tiny_http` to accept event-push HTTP requests (e.g., `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`) from the Stacks node, and processes them one at a time in a single-threaded loop. When handling a request, `process_event()` performs a blocking `request.as_reader().read_to_string(&mut body)` with no read timeout configured. A request whose declared `Content-Length` promises more bytes than are ever sent (or that is sent with an incomplete/never-finishing body) will cause this read to block forever, freezing `main_loop()` and preventing the receiver from accepting or processing any further events — functionally the same "single-threaded handler wedged by an incomplete request body" bug class as the OctoPrint advisory (CVE-2025-48879), just via a hung blocking read instead of a busy loop.

### Finding Description
`EventReceiver::main_loop()` [1](#0-0)  repeatedly calls `next_event()`, which itself calls `http_server.recv()` and then dispatches the parsed request to `process_event::<T,E>()` [2](#0-1) .

`process_event` reads the entire request body synchronously and unboundedly:
```
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
``` [3](#0-2) 

This call blocks on the underlying TCP socket until either the number of bytes promised by the request (via `Content-Length`, or the chunked terminator) has been fully received, or the connection errors/closes. There is no read timeout set anywhere in `events.rs` (no `set_read_timeout` calls were found in this module), and no bound is enforced on body size before/while reading.

Because `main_loop()` is a strictly serial loop — it calls `next_event()` (which blocks inside `process_event`) and only after that returns does it loop back to call `next_event()`/`http_server.recv()` again — a single client that opens a TCP connection, sends valid HTTP headers with `Content-Length: N` (or `Transfer-Encoding: chunked`), and then stalls (sends fewer than `N` bytes, or never sends the terminating chunk), causes the read to hang. Since `tiny_http`'s `recv()`/thread-pool for accepting new connections is decoupled from the single dequeue-and-process loop that this code performs, the receiver thread becomes permanently stuck servicing this one connection, and the whole event-receiver stops responding to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and even `/status` and `/shutdown` requests, since the graceful-shutdown path also relies on delivering a new HTTP request to wake up the loop [4](#0-3) .

This mirrors the OctoPrint bug class: an endpoint that reads a client-supplied HTTP body without enforcing a byte-count/time bound can be made to hang the single-threaded processing loop with a malformed/incomplete request, producing an availability outage for legitimate consumers.

### Impact Explanation
This endpoint is the channel through which the local Stacks node pushes stacker-DB chunk events, burn-block events, new-block events, and block-validation responses to the signer. If it can be reached by any host capable of opening a TCP connection to the bound address/port (which depends on operator configuration — the code itself performs no source-IP/auth check on incoming connections; see below), an attacker can trivially wedge the receiver, preventing the signer from learning about new stacker-DB chunks, block proposals, or burn-block ticks. This directly degrades the signer's liveness/availability, which is security-relevant for a component whose job is timely block validation/signing.

### Likelihood Explanation
Reaching this requires only the ability to open a TCP connection to the signer's event-receiver bind address and send a well-formed HTTP preamble with a `Content-Length` header (or `chunked` encoding) that is never fully satisfied — no cryptographic material, node secret, or privileged role is required. I could not verify from the available code whether operators are expected to bind this listener to loopback-only addresses by default or whether it is commonly exposed on a routable interface; the code itself does not enforce any peer-address restriction or authentication, so the risk depends on deployment configuration. This uncertainty should be validated further (e.g., by checking `stacks-signer` config defaults for `endpoint`/bind address) before treating this as unconditionally remotely exploitable.

### Recommendation
- Set an explicit read timeout on the underlying `tiny_http` server/socket (or on the per-request reader) so a stalled sender cannot block the receiver thread indefinitely.
- Enforce a maximum body size and abort/close the connection if it is exceeded or if the client stops sending data before the declared length is reached.
- Consider decoupling body-reading from the single serial event loop (e.g., handle each connection's read in its own thread/task with a timeout) so that one slow/malicious peer cannot block processing of subsequently-queued or newly-arriving events.
- If not already done, restrict the event-receiver bind address to loopback/trusted interfaces by default, and document this as a hard requirement in deployment guidance.

### Proof of Concept
1. Start a `stacks-signer` instance with its event-receiver bound to a reachable address/port (per its configuration).
2. From an attacking host, open a TCP connection to that address and send:
   ```
   POST /stackerdb_chunks HTTP/1.1\r\n
   Host: <victim>\r\n
   Content-Type: application/json\r\n
   Content-Length: 999999999\r\n
   \r\n
   ```
   then send only a few bytes of body (or none), and never close the connection or send the remaining declared bytes.
3. Observe that `process_event`'s `read_to_string` call blocks indefinitely, `main_loop()` never returns from `next_event()`, and subsequent legitimate node-pushed events (`/stackerdb_chunks`, `/new_burn_block`, `/new_block`, `/proposal_response`, `/status`) are not processed for as long as the malicious connection is held open — reproducing the same "single-threaded handler wedged by unfinished body" effect as the OctoPrint advisory.

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

**File:** libsigner/src/events.rs (L413-458)
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
