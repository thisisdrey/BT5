### Title
Signer Event Receiver HTTP Endpoint Accepts Unauthenticated Events From Any Source - (File: libsigner/src/events.rs)

### Summary
The `stacks-signer`'s `SignerEventReceiver` binds an HTTP server (`endpoint`) that receives node-originated events (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) and forwards them directly into the signer's `SignerRunLoop`. This handler performs no origin/source validation and no authentication check whatsoever — it accepts and processes any well-formed POST from any client that can reach the socket, exactly analogous to the DevSpace `CheckOrigin`-fails-open bug where any reachable client is treated as trusted.

### Finding Description
`SignerEventReceiver::bind` opens a plain `tiny_http` server on the configured `endpoint` socket address [1](#0-0) . `next_event()` then dispatches based solely on URL path and HTTP method, with zero checks on the caller's identity, source IP, or any shared secret/header: `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` are all routed straight into `process_event`, and `/status` is answered unconditionally [2](#0-1) . `process_event` simply reads the body and JSON-deserializes it into the corresponding event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`) and converts it into a `SignerEvent`, which is unconditionally forwarded to the signer runloop [3](#0-2) .

There is no `authorization` header check anywhere in this file (`grep` for `remote_addr`/auth in `libsigner/` returns nothing), unlike the equivalent node-side HTTP RPC handlers (`postblock_proposal.rs`, `fastcallreadonly.rs`, `blocksimulate.rs`, `blockreplay.rs`), which all gate access behind an `auth` password comparison before accepting input [4](#0-3) . The intended trust model is that only the local `stacks-node` (configured via `[[events_observer]] endpoint`) posts to this listener, and the docs explicitly instruct binding it to `0.0.0.0:<port>` in sample configs [5](#0-4) , and the runloop code carries only a *warning* comment about this risk rather than an enforced control [6](#0-5) .

This breaks the equality "event genuinely originated from the paired stacks-node" vs. "event accepted and acted on by the signer" — any network-reachable client can forge a `BlockValidationResponse`, `NewBurnBlock`, or `NewBlock` event and inject it, because the code never checks *who* sent the HTTP request, only *what path* was requested.

### Impact Explanation
This is an unauthenticated write into the signer's internal event stream/state (`SignerRunLoop`), reachable by anyone who can open a TCP connection to the configured `endpoint` — which the shipped sample configs explicitly recommend binding to `0.0.0.0`. Depending on deployment, this can range from local-privilege-boundary bypass (any local process on the host, not just the paired node) to full remote unauthenticated write if the operator follows the documented `0.0.0.0` binding without an additional firewall. Because `BlockValidationResponse` and burn/stacks-block notifications feed directly into signer decision inputs, forged events could desynchronize or corrupt the runloop's view of chain state fed from this channel.

### Likelihood Explanation
High under the shipped/documented configuration: the sample config explicitly sets `endpoint = "0.0.0.0:30000"` [5](#0-4) , and the code contains only a passive log warning about this exposure rather than any enforced restriction [7](#0-6) . No authentication mechanism (token, header, mTLS, or loopback-only binding enforcement) exists in `libsigner/src/events.rs` to mitigate this even when the operator does expose the port more broadly than intended.

### Recommendation
Add a required shared-secret/authorization header check in `SignerEventReceiver::next_event` (mirroring the `auth_token`/`auth` password pattern already used by `RPCBlockProposalRequestHandler` and friends in `stackslib/src/net/api/`), and/or enforce a default loopback-only bind with an explicit opt-in flag before allowing `0.0.0.0` binding for this listener. At minimum, document and default to binding on `127.0.0.1` and require operators to explicitly acknowledge the risk before allowing external binding.

### Proof of Concept
1. Configure and start a `stacks-signer` per `sample/conf/signer/mainnet-signer-conf.toml`, with `endpoint = "0.0.0.0:30000"` as documented.
2. From any host that can reach `<signer_ip>:30000` (not necessarily the paired stacks-node), send:
```
POST /proposal_response HTTP/1.1
Host: <signer_ip>:30000
Content-Type: application/json
Content-Length: <n>

{ ... forged BlockValidateResponse JSON ... }
```
3. `SignerEventReceiver::next_event` routes this to `process_event::<T, BlockValidateResponse>` without any identity check [8](#0-7) , and the resulting `SignerEvent::BlockValidationResponse` is forwarded into the signer's runloop [9](#0-8) , demonstrating that a forged, non-node-originated event is accepted and processed exactly as if it came from the trusted paired node.

### Citations

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

**File:** libsigner/src/events.rs (L469-490)
```rust
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
        } else {
            for (i, out_channel) in self.out_channels.iter().enumerate() {
                if let Err(e) = out_channel.send(ev.clone()) {
                    error!("Failed to send to signer runloop #{}: {:?}", i, &e);
                    return false;
                }
            }
            true
        }
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

**File:** stackslib/src/net/api/postblock_proposal.rs (L1135-1144)
```rust
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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** stacks-signer/src/lib.rs (L124-132)
```rust
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
