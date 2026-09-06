### Title
Signer event-receiver HTTP endpoint accepts unauthenticated event injection - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver::next_event()` handler that a `stacks-signer` process runs to receive `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, and `StacksBlockEvent` payloads from a stacks-node performs no authentication whatsoever on inbound HTTP POSTs. Any host that can reach the bound TCP port can push arbitrary, fully-forged events directly into the signer's trusted processing pipeline, exactly analogous to the vLLM `XPUB` socket accepting connections/data from any network peer with no authentication gate.

### Finding Description
`SignerEventReceiver::bind()` opens a plain `tiny_http` server on the configured `endpoint` (sample configs even ship `endpoint = "0.0.0.0:30000"`), with no allow-list, TLS client cert, or bearer-token check: [1](#0-0) 

`next_event()` then dispatches purely on URL path and HTTP method, deserializing the request body straight into a trusted `SignerEvent` with no signature or credential verification of the sender: [2](#0-1) 

This is a genuine equality break: the event is treated as "came from my configured stacks-node" (authenticated-by-topology) when it is in fact merely "received bytes from a TCP port" (unauthenticated). Contrast this with the *node*-side endpoint (`postblock_proposal.rs`) and `httpcore.rs`, which do implement `auth_token`/`Authorization` header checks for requests coming *into* the node — there is no symmetric check on requests coming *into* the signer's event receiver. `libsigner/src/http.rs` only adds `Authorization` headers on *outbound* requests the signer makes to the node; it does not gate what the signer's own listener accepts.

The mismatch is called out only as an operational warning, not enforced in code: [3](#0-2) 

### Impact Explanation
An attacker with network reachability to the signer's `endpoint` (which the sample configs recommend binding to `0.0.0.0`) can POST forged `/stackerdb_chunks`, `/proposal_response`, `/new_block`, or `/new_burn_block` bodies. These are decoded and forwarded via `forward_event()` straight into the signer's runloop channel as if they were legitimate node-originated events: [4](#0-3) 

This is an unauthenticated write into the signer's internal event/state stream — a Critical-class primitive per the rules ("unauthenticated/unauthorized write to state"). What the signer runloop subsequently does with forged burn-block/new-block/proposal-response notifications is out of scope (signer decision logic), but the injection vector itself — the transport/listener in `libsigner` — is squarely in scope and is the fault site.

### Likelihood Explanation
High likelihood if the signer's `endpoint` is reachable beyond localhost (which the shipped `mainnet-signer-conf.toml` example explicitly configures as `0.0.0.0:30000`). No secrets, node key, or signer key are required — only a `POST` request to a known, discoverable path.

### Recommendation
Require the same shared-secret validation used elsewhere (`auth_token`/`auth_password`) to be enforced on inbound requests to `SignerEventReceiver`, e.g., checking an `Authorization` header against the configured `auth_password` before calling `process_event`, and/or defaulting `endpoint` binding to loopback-only with an explicit opt-in for wider binding.

### Proof of Concept
1. Deploy a signer with `endpoint = "0.0.0.0:30000"` (per the sample config).
2. From any host that can route to port 30000, send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"contract_id": "...", "modified_slots": [<forged StackerDBChunkData>]}
```
3. `next_event()` accepts and decodes this body with no credential check and forwards it into the signer runloop as a legitimate `SignerEvent::SignerMessages`/`MinerMessages` event, confirmed by the existing test harness pattern that drives events purely by connecting a `TcpStream` and POSTing, with no auth header at all: [5](#0-4)

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

**File:** libsigner/src/events.rs (L410-458)
```rust
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

**File:** libsigner/src/tests/mod.rs (L120-146)
```rust
    // simulate a node that's trying to push data
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
