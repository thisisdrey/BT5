### Title
Unbounded, unauthenticated HTTP body read in `process_event` causes memory-exhaustion DoS on signer event ingestion - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` dispatches any POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` to `process_event`, which calls `request.as_reader().read_to_string(&mut body)` with no length cap before any `serde_json` parsing or authentication occurs. Because the signer's event listener performs no authentication check on incoming POSTs and (per the shipped sample configs) is commonly bound to `0.0.0.0`, any network party able to reach that port can send a request with an attacker-chosen `Content-Length` and stream an unbounded amount of data, causing the single-threaded event-receiver loop to allocate memory proportional to the attacker's declared body size and to stall on that one connection.

### Finding Description
`process_event` at [1](#0-0)  reads the entire HTTP body into a `String` via `read_to_string` before doing any authentication, size validation, or JSON parsing:

```rust
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
```

There is no equivalent of `MAX_PAYLOAD_LEN`/content-length cap here (contrast with the node's own `stackslib/src/net/http` handlers, which enforce explicit payload-size limits before buffering). The dispatch site, `SignerEventReceiver::next_event`, routes any POST whose URL matches one of the known paths straight into `process_event` with zero authentication check — there is no verification of an auth token/secret against the request at all in this file [2](#0-1) . The `auth_password`/`auth_token` pairing documented in the sample configs governs the signer's outbound calls to the node's RPC (block-proposal validation) endpoint, not inbound POSTs to the signer's own event-listener port [3](#0-2) .

The signer's event-listener `endpoint` is explicitly documented/shipped as binding on all interfaces (`endpoint = "0.0.0.0:30000"`) in the reference signer config [4](#0-3) , and `bind()` passes that address directly to `HttpServer::http(listener)` with no source-IP allow-list or credential check [5](#0-4) . Any remote host that can route to that port can therefore connect directly (bypassing the node entirely) and send a POST to `/stackerdb_chunks` (or the other paths) with a large declared `Content-Length` and a slow/large body; `read_to_string` will keep growing `body` without any bound, and since `next_event`/`main_loop` processes one request at a time in a single thread [6](#0-5) , this also stalls ingestion of legitimate node-pushed events (StackerDB chunks, block-validation responses, burn-block notifications) for the duration of the attack.

### Impact Explanation
An attacker can force unbounded memory allocation on the signer process and monopolize its single-threaded event-ingestion loop with one connection, denying processing of legitimate events from the node (StackerDB chunks driving block signing, burn-block/tenure events) and/or exhausting host memory. This is an unauthenticated remote DoS reachable with a single crafted request, matching the Critical category ("remote crash/unauthenticated DoS from few messages"). It affects a single signer node, so the direct blast radius is per-signer, but repeated against all signers in a set could degrade network-wide signing availability.

### Likelihood Explanation
No secret, key, or privileged role is required — the sample deployment configuration binds the event-listener to `0.0.0.0`, and there is no authentication check on the inbound event POST path in `events.rs`. The attacker only needs network reachability to the bound port and can repeat the attack at will and cheaply (a single slow POST with a large `Content-Length`).

### Recommendation
- Enforce a maximum request-body length in `process_event` (e.g., check `Content-Length` header against a constant such as a new `MAX_EVENT_BODY_LEN`, and/or wrap the reader in a bounded `Read::take(...)`) before calling `read_to_string`, rejecting oversized requests early.
- Require the event-listener to authenticate the sender (e.g., shared-secret header check) before doing any body reads, and/or bind the listener to loopback-only by default with an explicit opt-in warning for `0.0.0.0`.

### Proof of Concept
Rust test plan (added to `libsigner/src/tests/mod.rs`):
1. Spawn a `SignerEventReceiver<SignerMessage>` via `Signer::spawn` bound to `127.0.0.1:<port>` as in existing tests (`test_status_endpoint`).
2. From a separate thread, open a raw `TcpStream` to the bound port and send:
   `POST /stackerdb_chunks HTTP/1.1\r\nHost: ...\r\nContent-Length: 5000000000\r\n\r\n` followed by writing bytes in a loop indefinitely (or a very large buffer) without ever completing 5GB.
3. Observe/assert that the signer process's memory usage for the `body` allocation grows unbounded (e.g., instrument with a custom `Read` wrapper counting bytes buffered) and that `next_event` never returns/timeouts, blocking subsequent legitimate `/stackerdb_chunks` POSTs from being processed — i.e., assert the call in `process_event::<SignerMessage, StackerDBChunksEvent>` is NOT bounded/rejected, contrary to expected behavior of capping body size before allocation.

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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L45-50)
```text
# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
