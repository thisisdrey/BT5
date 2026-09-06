### Title
Signer event HTTP listener (`SignerEventReceiver`) accepts unauthenticated POSTs, allowing remote unauthenticated shutdown and forged event injection - ([File: libsigner/src/events.rs])

### Summary
The signer-side event HTTP server implemented by `SignerEventReceiver::next_event` in `libsigner/src/events.rs` accepts and processes POST requests on `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/shutdown` from any TCP client that can reach the bound listener, with no authentication check whatsoever. Because sample deployment configs bind this listener to `0.0.0.0` [1](#0-0) , any remote, unprivileged party that can reach the port can either terminate the signer process on demand or inject forged signer-runloop events that are normally trusted to originate only from the paired stacks-node.

### Finding Description
`SignerEventReceiver::next_event` dispatches incoming HTTP requests purely by URL path, with no credential, token, or peer-identity check: [2](#0-1) 

Notably:
- `/shutdown` immediately sets `stop_signal` to `true` and returns `EventError::Terminated`, which causes `EventReceiver::main_loop` to exit [3](#0-2) , [4](#0-3) .
- `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` are routed straight to `process_event`, which reads the raw JSON body and directly deserializes it into a trusted event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`), with no verification that the request came from the configured stacks-node: [5](#0-4) .

Unlike the stacks-node's own RPC/event-dispatch side, which supports an `auth_token`/`auth_password` pairing documented in the sample configs (`[connection_options] auth_token` on the node side, `auth_password` on the signer binary side) [6](#0-5) , [7](#0-6) , that auth token is never checked anywhere in `libsigner`'s HTTP handling path (`libsigner/src/events.rs`, `libsigner/src/http.rs`). A `grep` for `auth_token`/`Authorization` handling inside `libsigner/**` and `stacks-node/src/event_dispatcher*/**` returns no results — the token exists only for signer→node communication (block-proposal RPC), not for node→signer event delivery. The listener that receives events is therefore an auth-gate that fails open: it is documented/intended to only be reachable by the paired node, but nothing in the code enforces that expectation, and the bind address in the reference configs is `0.0.0.0`.

This breaks the intended equality "event came from the trusted node" vs. "event came from any TCP peer". A remote, unprivileged attacker who can route packets to the signer's bound port can:
1. POST to `/shutdown` to unconditionally kill the signer's event-receiver thread (and, since `main_loop` exits, effectively stop the signer from receiving further node events), a trivial unauthenticated DoS requiring a single HTTP request.
2. POST forged JSON bodies to `/stackerdb_chunks`, `/new_burn_block`, `/new_block`, or `/proposal_response` that get deserialized and forwarded into the signer's `SignerRunLoop` as if they were genuine node-originated events, since `process_event` performs no origin check before converting the JSON into a `SignerEvent<T>` and handing it to `forward_event`.

### Impact Explanation
This qualifies as remote, unauthenticated DoS from a few messages (the `/shutdown` path) and unauthenticated/unauthorized "write" into the signer's internal state (forged event injection feeding the signer runloop), matching the Critical impact bar. A malicious actor does not need the node's key, the signer's key, or any admin role — only network reachability to the signer's listening socket.

### Likelihood Explanation
High likelihood in any deployment following the shipped reference configuration, since the sample configs bind the signer's event endpoint to `0.0.0.0` and provide no other network-layer isolation guarantee in the code itself; the vulnerability requires no cryptographic material and only network reachability.

### Recommendation
Add an authentication check (e.g., a shared secret/HMAC or token verification against the node's configured `auth_token`) inside `SignerEventReceiver::next_event` in `libsigner/src/events.rs` before dispatching to `process_event` or honoring `/shutdown`, and reject unauthenticated requests. Consider also restricting the default bind address away from `0.0.0.0` unless explicitly opted into, and documenting/enforcing that this listener must not be exposed to untrusted networks.

### Proof of Concept
1. Deploy a stacks-signer using the sample reference config, which binds `endpoint = "0.0.0.0:30000"` with no additional authentication on the event-receiver side.
2. From a remote unprivileged host, run:
```
curl -X POST http://<signer-ip>:30000/shutdown
```
This request needs no valid credential; per `libsigner/src/events.rs` lines 443-445, it unconditionally sets `stop_signal` and terminates the event receiver.
3. Alternatively, POST a forged JSON body to `/new_burn_block` or `/stackerdb_chunks` — `process_event` (lines 519-542) will deserialize it and forward a corresponding `SignerEvent` into the signer's runloop with no verification that it originated from the paired stacks-node.

### Citations

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

**File:** libsigner/src/events.rs (L413-457)
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
```

**File:** libsigner/src/events.rs (L519-542)
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

    let signer_event: SignerEvent<T> = json_event.try_into()?;

    Ok(signer_event)
}
```

**File:** sample/conf/mainnet-signer.toml (L9-11)
```text
# Key coordination points between this config and the signer binary:
#   - [[events_observer]] endpoint must match signer's `endpoint`
#   - [connection_options] auth_token must match signer's `auth_password`
```
