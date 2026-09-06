### Title
Unauthenticated event-injection into `stacks-signer` via `SignerEventReceiver`'s plaintext HTTP listener - (File: `libsigner/src/events.rs`)

### Summary
The `stacks-signer` binary exposes an HTTP listener (`SignerEventReceiver`, meant to receive event-observer callbacks from a trusted `stacks-node`) that accepts and processes POST requests from **any** TCP client that can reach the configured `endpoint` (by default bound to `0.0.0.0:<port>` per the sample configs), with **no authentication, no origin check, and no peer verification** of any kind.

### Finding Description
`SignerEventReceiver::bind` simply opens a `tiny_http::Server` on the configured address [1](#0-0) , and `next_event` dispatches incoming requests purely by URL path (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`) with no authorization/header check whatsoever [2](#0-1) . `process_event` merely reads the body and deserializes it into the target JSON type before converting it into a `SignerEvent` [3](#0-2) .

Critically, for `BlockValidateResponse` (`/proposal_response`) and `BurnBlockEvent` (`/new_burn_block`), the `TryFrom` conversions perform **no cryptographic or provenance validation at all** — they just wrap the deserialized fields directly into a `SignerEvent` [4](#0-3) . This is confirmed by the test harness itself, which demonstrates that a bare, unauthenticated `TcpStream::connect` + raw HTTP POST is sufficient to inject events into the signer runloop [5](#0-4) , and that even `/status` requires no credentials [6](#0-5) .

The `/shutdown` path also terminates the receiver's main loop unconditionally on receipt [7](#0-6) .

This is analogous to the Jenkins CSWSH bug class in spirit (an endpoint that implicitly trusts its caller without verifying who is actually connecting) but is in fact a strictly worse variant: Jenkins at least required a same-origin browser session to be hijacked, whereas here there is **no authentication mechanism of any kind** protecting this listener — any host that can route a TCP packet to the bound port can forge node-originated signer events.

By contrast, every comparable HTTP surface in `stackslib/src/net/api/**` (block proposal, block replay, block simulate, fast-call-read-only, tx simulate, `/v3/blocks?broadcast=1`) explicitly checks an `Authorization` header against a configured shared secret before accepting the request [8](#0-7) [9](#0-8) . The signer's own event-listener endpoint (which drives its state machine and, in the case of `BlockValidateResponse`, potentially its signing decisions) has no equivalent gate at all.

### Impact Explanation
This maps to the "Critical - unauthenticated/unauthorized write to state, network-wide propagation of forged data" impact bucket: a remote, unauthenticated attacker who can reach the signer's `endpoint` (documented default `0.0.0.0:<port>`, e.g. `0.0.0.0:30000`, in `sample/conf/signer/mainnet-signer-conf.toml` and `sample/conf/mainnet-signer.toml`) can:
- Inject forged `BurnBlockEvent`/`NewBurnBlock` state into the signer's runloop, corrupting its view of chain progress.
- Inject forged `BlockValidateResponse` ("proposal_response") events that the signer's runloop consumes as if the local, trusted node had validated a block.
- Terminate the signer process at will via `/shutdown`, causing denial of service against the signer without any credentials.

Whether this translates into an actual bad signature (i.e., a broken tenure) depends on validation performed later inside the signer's decision-making runloop (out of scope per the rules), but the transport-layer trust boundary itself — the thing in scope (`libsigner` transport files) — is broken: it fails open with zero authentication.

### Likelihood Explanation
Likelihood depends heavily on deployment network exposure. The bundled reference configs bind the listener to `0.0.0.0`, and the code path itself contains no capability to restrict callers by IP/token — it is a pure function of "can you open a TCP connection to this port." Any signer operator who does not add an external firewall/reverse-proxy in front of this port is exposed to remote, unauthenticated event injection and shutdown. This is not a volumetric/DDoS issue — a single well-formed POST is sufficient.

### Recommendation
Add an authentication/authorization gate to `SignerEventReceiver::next_event`/`process_event` analogous to the `authorization` header checks already used in `stackslib/src/net/api/**` (e.g., require and validate a shared-secret header, or restrict binding to loopback by default with an explicit opt-in for wider binding), and reject `/shutdown` and event-processing requests from unauthenticated callers.

### Proof of Concept
Using the existing test pattern in `libsigner/src/tests/mod.rs` as a template: connect a raw `TcpStream` to the signer's configured `endpoint` and send a hand-crafted HTTP POST to `/proposal_response`, `/new_burn_block`, or `/shutdown` with an arbitrary JSON body — no `Authorization` header or any other credential is required, and the request is accepted and forwarded to the signer runloop exactly as the test `test_simple_signer` demonstrates for `/stackerdb_chunks` [5](#0-4) .

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

**File:** libsigner/src/events.rs (L627-649)
```rust
impl<T: SignerEventTrait> TryFrom<BlockValidateResponse> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(block_validate_response: BlockValidateResponse) -> Result<Self, Self::Error> {
        Ok(SignerEvent::BlockValidationResponse(
            block_validate_response,
        ))
    }
}

impl<T: SignerEventTrait> TryFrom<BurnBlockEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(burn_block_event: BurnBlockEvent) -> Result<Self, Self::Error> {
        Ok(SignerEvent::NewBurnBlock {
            burn_height: burn_block_event.burn_block_height,
            received_time: SystemTime::now(),
            burn_header_hash: burn_block_event.burn_block_hash,
            consensus_hash: burn_block_event.consensus_hash,
            parent_burn_block_hash: burn_block_event.parent_burn_block_hash,
        })
    }
}
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

**File:** libsigner/src/tests/mod.rs (L199-222)
```rust
#[test]
fn test_status_endpoint() {
    let ev = SignerEventReceiver::new(false);
    let (res_send, _res_recv) = channel();
    let max_events = 1;
    let mut signer = Signer::new(SimpleRunLoop::new(max_events), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:31000".parse().unwrap();

    // Spawn the signer first so the HTTP server is listening before the mock client connects
    let running_signer = signer.spawn(endpoint).unwrap();

    // simulate a node that's trying to push data
    let mock_stacks_node = thread::spawn(move || {
        let mut sock = TcpStream::connect(endpoint).unwrap();
        let req = format!("GET /status HTTP/1.1\r\nHost: {endpoint}\r\nConnection: close\r\n\r\n");

        sock.write_all(req.as_bytes()).unwrap();
        let mut buf = [0; 128];
        let _ = sock.read(&mut buf).unwrap();
        let res_str = std::str::from_utf8(&buf).unwrap();
        let expected_status_res = "HTTP/1.1 200 OK\r\n";
        assert_eq!(expected_status_res, &res_str[..expected_status_res.len()]);
        sock.flush().unwrap();
    });
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1136-1144)
```rust
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

**File:** stackslib/src/net/api/postblock_v3.rs (L99-122)
```rust
        // if broadcast=1 is set, then the requester must be authenticated
        let mut broadcast = false;
        let mut authenticated = false;

        // look for authorization header
        if let Some(password) = &self.auth {
            if let Some(auth_header) = preamble.headers.get("authorization") {
                if auth_header != password {
                    return Err(Error::Http(401, "Unauthorized".into()));
                }
                authenticated = true;
            }
        }

        // see if broadcast=1 is set
        for (key, value) in form_urlencoded::parse(query.as_ref().unwrap_or(&"").as_bytes()) {
            if key == "broadcast" {
                broadcast = broadcast || value == "1";
            }
        }

        if broadcast && !authenticated {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```
