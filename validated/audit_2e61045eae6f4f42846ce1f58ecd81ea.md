## Finding

### Title
Signer event-receiver HTTP endpoint accepts unauthenticated POST events from any network peer, allowing forged event injection and remote shutdown - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` runs an HTTP server (bound, per sample configs, to `0.0.0.0:<port>`) that is meant to receive event pushes only from its paired, trusted `stacks-node`. Unlike the node's own inbound RPC endpoints (e.g. `/v3/block_proposal`), which gate on a shared `authorization` header/`auth_token`, this listener performs **no authentication check whatsoever** before accepting and forwarding `POST` events into the signer's runloop, or before honoring an unauthenticated `/shutdown` request.

### Finding Description
The event receiver binds a plain HTTP server via `tiny_http`: [1](#0-0) 

and its request-dispatch loop routes any incoming request purely by URL/method, with no credential check of any kind: [2](#0-1) 

Compare this to the node-side equivalent, `RPCBlockProposalRequestHandler`, which the same signer/node pairing uses in the opposite direction and which explicitly requires a matching `authorization` header before parsing any payload: [3](#0-2) 

The node-side configuration docs make the trust asymmetry explicit — `auth_token`/`auth_password` is documented as securing "the communication channel between this node and a connected `stacks-signer` instance" and the node's `/v3/block_proposal` endpoint, but there is no corresponding mechanism protecting the signer's own inbound listener: [4](#0-3) 

Sample configs confirm the signer's event endpoint is bound broadly (`0.0.0.0:30000`) with the coordination contract being "endpoint must match" — i.e., a shared address, not a shared secret: [5](#0-4) 

Because there is no equality check binding the sender to a trusted identity (no token, no signature, no IP allowlist), any unprivileged remote host that can reach the bound port can:
1. `POST /stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` with attacker-controlled bodies, which are decoded and forwarded directly into the signer's processing channel via `forward_event`: [6](#0-5) 
2. `POST /shutdown`, which unconditionally sets the stop flag and terminates the event receiver with a single unauthenticated request: [7](#0-6) 

This is the CWE-668 "wrong sphere" pattern from the report: a resource (the signer's inbound event API) that is only supposed to be reachable from one trusted counterpart (the paired stacks-node) is instead exposed to the whole network sphere it's bound on, without the access-control check its sibling endpoint (`/v3/block_proposal`) enforces.

### Impact Explanation
- Unauthenticated remote DoS: a single `POST /shutdown` from any reachable host terminates the signer's event-receiving server, matching the "Critical - remote crash/unauthenticated DoS from few messages" bar.
- Unauthorized injection of events into the signer's runloop: forged `/new_burn_block`, `/new_block`, or `/proposal_response` payloads reach the signer's internal channel as if legitimately sourced from the paired node, without any sender authentication — an unauthenticated write into signer-observed state.

### Likelihood Explanation
Any host that can open a TCP connection to the configured signer endpoint (which sample configs bind to `0.0.0.0`) can trigger this with a trivially crafted raw HTTP request; no cryptographic material, node secret, or privileged role is required, and `decode_http_request`/`process_event` only validate wire-format, not origin.

### Recommendation
Require the same shared-secret `authorization` check on the signer's inbound event endpoint that the node enforces on `/v3/block_proposal` (reusing `auth_token`/`auth_password`), reject requests lacking a valid credential before dispatching to `process_event`, and gate `/shutdown` behind the same check (or a loopback-only bind) rather than accepting it unauthenticated.

### Proof of Concept
```
nc <signer-host> 30000
POST /shutdown HTTP/1.1
Host: <signer-host>
Connection: close

```
Sending this single unauthenticated request causes `next_event` to hit the `/shutdown` branch and terminate the receiver: [7](#0-6) . Similarly, `POST /new_burn_block` (or the other recognized paths) with an arbitrary body is decoded by `process_event` and forwarded to the signer runloop with no verification of the sender.

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
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

**File:** libsigner/src/events.rs (L466-480)
```rust
    /// Forward an event
    /// Return true on success; false on error.
    /// Returning false terminates the event receiver.
    fn forward_event(&mut self, ev: SignerEvent<T>) -> bool {
        if self.out_channels.is_empty() {
            // nothing to do
            error!("No channels connected to event receiver");
            false
        } else if self.out_channels.len() == 1 {
            // avoid a clone
            if let Err(e) = self.out_channels[0].send(ev) {
                error!("Failed to send to signer runloop: {:?}", &e);
                return false;
            }
            true
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1128-1144)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block proposal endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/config/mod.rs (L3802-3816)
```rust
    /// HTTP auth password to use when communicating with stacks-signer binary.
    ///
    /// This token is used in the `Authorization` header for certain requests.
    /// Primarily, it secures the communication channel between this node and a
    /// connected `stacks-signer` instance.
    ///
    /// It is also used to authenticate requests to `/v2/blocks?broadcast=1`.
    /// ---
    /// @default: `None` (authentication disabled for relevant endpoints)
    /// @notes:
    ///   - This field **must** be configured if the node needs to receive
    ///     block proposals from a configured `stacks-signer` [[events_observer]]
    ///     via the `/v3/block_proposal` endpoint.
    ///   - The value must match the token configured on the signer.
    pub auth_token: Option<String>,
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L36-39)
```text

# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```
