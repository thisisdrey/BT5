### Title
Unauthenticated remote client can stall the signer's event receiver by holding open a slow/incomplete HTTP connection - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` (used by `EventReceiver::main_loop`) is a strictly single-threaded, serial request-processing loop built on `tiny_http`. It repeatedly calls `http_server.recv()` and then, for POST bodies, `request.as_reader().read_to_string(&mut body)`, with no per-connection read timeout configured anywhere in this path. Because the sample/reference configuration explicitly recommends binding this receiver to `0.0.0.0:<port>` (network-reachable, unauthenticated — there is no auth check before `next_event()` reads the body), a remote client that opens a TCP connection to the signer's event port and sends a partial/slow request (or none at all) can block the single-threaded receive loop indefinitely, exactly the "serial processing of TCP queries" flaw described in the aardvark-dns advisory.

### Finding Description
`EventReceiver::main_loop` (`libsigner/src/events.rs:284-312`) calls `self.next_event()` in a tight loop with no concurrency: [1](#0-0) 

`next_event()` blocks on `http_server.recv()` and, for any recognized POST path, hands the request to `process_event`, which calls `request.as_reader().read_to_string(&mut body)`: [2](#0-1) [3](#0-2) 

There is no `set_read_timeout`/`with_timeout` configuration anywhere in `libsigner/src/events.rs` for the `HttpServer` (`tiny_http::Server`) — the only `bind()` call is a bare `HttpServer::http(listener)`: [4](#0-3) 

Because `main_loop` never spawns per-connection handling and there is no timeout on reading the request body, a single client that connects and sends bytes slowly (or a partial `Content-Length` body that never completes) will keep `read_to_string` blocked forever, or keep the internal request queue from advancing. While this thread is stuck, `next_event()` never returns, so the legitimate Stacks node's event POSTs (`stackerdb_chunks`, `block_proposal`, `proposal_response`, `new_burn_block`, `new_block`) queued behind it are not processed. This is the equality the report's bug class targets: "one client's held-open connection blocks processing for all others" — the same design defect as CVE-2024-8418 (serial TCP handling in aardvark-dns).

The sample/reference configuration for this exact code path recommends exposing the endpoint on all interfaces, making it remotely reachable without any authentication step preceding the body read: [5](#0-4) 

### Impact Explanation
If the signer's event endpoint is bound to a non-loopback address (as the shipped reference config instructs), any unauthenticated remote peer can open a TCP connection and hold it open with a slow/partial request. Because the receive loop is single-threaded and unbounded by a timeout, this can indefinitely delay delivery of block-proposal, burn-block, and StackerDB-chunk events from the node to the signer process. A stalled signer can miss block-proposal validation deadlines and StackerDB chunk updates, degrading its ability to participate in signing rounds — a remote, unauthenticated availability impact on the signer process, matching CWE-400/"Uncontrolled Resource Consumption" and the CVSS vector's `AV:N/AC:L/PR:N/UI:N/A:H` profile. This is a design-level DoS (requires only one held-open connection, not volume), so it is not excluded by the "traffic volume" exclusion in scope rules.

### Likelihood Explanation
Likelihood is high wherever the event-receiver port is reachable from outside `127.0.0.1` (which the project's own sample config for mainnet signer operation configures by default: `endpoint = "0.0.0.0:30000"`). No authentication, valid signature, or special privilege is required — merely opening a TCP connection and trickling/withholding bytes of an HTTP request. This requires no exploitation of cryptography or consensus logic, only exploiting the blocking, single-threaded `recv()`/`read_to_string()` path.

### Recommendation
- Configure `tiny_http::Server`/its accepted streams with an explicit, short read/write timeout (`set_read_timeout` on the underlying stream, or use `tiny_http`'s timeout-aware `recv_timeout`), so a stalled client cannot block the loop indefinitely.
- Process each accepted connection/request on its own worker thread (or a small bounded thread/task pool) instead of a single serial loop, so one slow client cannot delay all others.
- Recommend/default-bind the event receiver to `127.0.0.1` in documentation and sample configs unless firewalled, and consider requiring the same `auth_password`/token check to gate connection acceptance before allowing a body read.

### Proof of Concept
1. Deploy `stacks-signer` with `endpoint = "0.0.0.0:30000"` as recommended by `sample/conf/signer/mainnet-signer-conf.toml`.
2. From a remote host, open a raw TCP connection to port 30000 and send only:
   `POST /stackerdb_chunks HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n` then send no further bytes (or trickle one byte every few seconds).
3. `SignerEventReceiver::next_event` (`libsigner/src/events.rs:413-459`) will block in `process_event`'s `read_to_string` (`libsigner/src/events.rs:526`) waiting for the promised body.
4. Meanwhile, have the actual Stacks node POST a legitimate `block_proposal`/`stackerdb_chunks` event to the same endpoint; because the loop in `main_loop` is serial and blocked, the legitimate event is not read/forwarded until the malicious connection times out or is closed by the OS — demonstrating the stall. [1](#0-0) [6](#0-5) [3](#0-2) [5](#0-4)

### Citations

**File:** libsigner/src/events.rs (L284-311)
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
```

**File:** libsigner/src/events.rs (L404-459)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }

    /// Wait for the node to post something, and then return it.
    /// Errors are recoverable -- the caller should call this method again even if it returns an
    /// error.
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

**File:** libsigner/src/events.rs (L519-537)
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
    // Regardless of whether we successfully deserialize, we should ack the dispatcher so they don't keep resending it
    ack_dispatcher(request);
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-50)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"

# REQUIRED: Network selection.
# Valid values: "mainnet", "testnet", "mocknet"
network = "mainnet"

# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
