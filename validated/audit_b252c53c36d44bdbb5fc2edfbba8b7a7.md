### Title
Unauthenticated event HTTP server allows forged StackerDB/block-proposal-response/burn-block events to be injected into the signer runloop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver` runs a plain HTTP server (`next_event`) that accepts unauthenticated `POST` requests on `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block`, deserializes the JSON body directly into signer-trusted events, and forwards them into the signer's runloop with no verification that the sender is the paired stacks-node.

### Finding Description
The signer's event-ingestion endpoint has no authentication gate at all, in contrast to every analogous node-side RPC handler in this codebase (`postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`), which all require an `authorization` header that exactly matches a configured `auth_token`/`auth_password` before parsing the request body [1](#0-0) .

`SignerEventReceiver::next_event` dispatches purely on URL path with no header/token check whatsoever: [2](#0-1) 

The dispatched handler `process_event` simply reads the body and deserializes it into the trusted event type (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `StacksBlockEvent`) with no signature or credential check: [3](#0-2) 

These events are then unconditionally forwarded to the signer's internal channel/runloop: [4](#0-3) 

This is the "auth-gate fails open" analog of the LooksRare timelock report: the documented/expected security boundary (node↔signer channel secured by `auth_token`/`auth_password`, per `docs/signing.md` and `sample/conf/signer/mainnet-signer-conf.toml`) is enforced only on the *node's* RPC endpoints that receive data from the signer/miner (`auth_token` checked in `postblock_proposal.rs`, `postblock_v3.rs`, `blockreplay.rs`, `blocksimulate.rs`), but the reverse direction — the *signer's* own listening endpoint that receives events pushed by the node — has no such check implemented anywhere in `libsigner/src/events.rs`. The documented `auth_password`/`auth_token` pairing gives the impression that the entire node↔signer channel is authenticated in both directions, but the code only guards one side.

Grep of `libsigner/src/*.rs` for `auth|password|token` confirms zero references to authentication in the transport/event code (only test files reference unrelated "signer_state" strings), confirming the gate is entirely absent, not merely misconfigured.

### Impact Explanation
Anyone who can reach the signer's `endpoint` (bound host:port, e.g. `0.0.0.0:30000` per sample config) can POST a forged `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` payload, which the signer will accept as if it came from the paired, trusted stacks-node. Because `SignerEvent::BlockValidationResponse`, `NewBurnBlock`, and `NewBlock` variants directly drive the signer's operational state (e.g. proposal validation results, burn/tenure tracking), an attacker on the same network segment (or one exposed via misconfiguration/NAT) can inject spoofed events that the signer will process with full trust, without the node ever having sent them — an unauthenticated write into the signer's decision-input pipeline. This matches the "unauthenticated/unauthorized write to state" and "forged data propagation" categories.

### Likelihood Explanation
The `bind()` call opens a plain `tiny_http` server with no TLS and no credential requirement; the sample configs bind to `0.0.0.0`, and the code's own warning acknowledges the risk ("communicating with an external node... could expose sensitive functionalities... if additional proper security checks are not integrated") [5](#0-4)  — but no such check exists in the transport layer itself. Any network position that can reach the bound port can trigger this with a single crafted HTTP POST; no secrets, timing, or races are required.

### Recommendation
Add an authentication check to `SignerEventReceiver::next_event` (or `process_event`) analogous to the node-side `RPCBlockProposalRequestHandler`/`RPCNakamotoBlockReplayRequestHandler` pattern: require the incoming request to carry an `authorization` header matching a configured shared secret (reusing the existing `auth_token`/`auth_password` concept) before accepting and deserializing `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` payloads, rejecting with 401 otherwise.

### Proof of Concept
1. Start a signer with `SignerEventReceiver::bind` listening on `0.0.0.0:<port>` (default sample configs use `0.0.0.0:30000`).
2. From any host that can reach `<port>` (no credentials needed), send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:<port>
Content-Type: application/json
Content-Length: <n>

{ ...forged BlockValidateResponse JSON... }
```
3. `next_event` routes this to `process_event::<T, BlockValidateResponse>(request)` at [6](#0-5) , which deserializes and forwards it with no authentication check, exactly as the existing test `test_simple_signer` demonstrates for legitimate traffic (using `TcpStream::connect` + raw HTTP POST with no auth header) [7](#0-6) .

### Citations

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

**File:** libsigner/src/events.rs (L466-490)
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

**File:** libsigner/src/events.rs (L517-542)
```rust
// TODO: add tests from mutation testing results #4835
#[cfg_attr(test, mutants::skip)]
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

**File:** stacks-signer/src/lib.rs (L119-132)
```rust
impl<S: Signer<T> + Send + 'static, T: SignerEventTrait + 'static> SpawnedSigner<S, T> {
    /// Create a new spawned signer
    pub fn new(config: GlobalConfig) -> Self {
        let endpoint = config.endpoint;
        info!("Stacks signer version {:?}", VERSION_STRING.as_str());
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
