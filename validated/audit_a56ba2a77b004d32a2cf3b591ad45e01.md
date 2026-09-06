Confirmed: no source-IP allowlist, no auth header check, no token verification anywhere in `SignerEventReceiver::next_event()` or `process_event()` — it accepts any POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block` from any TCP client that can reach the bound port, which per the reference config defaults to `0.0.0.0:30000` (all interfaces). [1](#0-0) 

### Title
Unauthenticated stacks-signer event HTTP listener accepts forged node events from any network peer - (File: libsigner/src/events.rs)

### Summary
The `stacks-signer` binary runs an HTTP server (`SignerEventReceiver`) that is meant to receive events exclusively from its paired, trusted `stacks-node` via the node's `events_observer` push mechanism. This listener performs no authentication of the caller and, per the reference config, binds to `0.0.0.0`, so any host that can route packets to the signer's port can submit forged `StackerDBChunksEvent`, `BlockValidateResponse` ("proposal_response"), `BurnBlockEvent`, and `StacksBlockEvent` payloads, which are accepted and forwarded into the signer's runloop as if they came from the legitimate node.

### Finding Description
`SignerEventReceiver::bind` opens a plain `tiny_http` server on the configured socket with no TLS and no credential requirement: [2](#0-1) 

`next_event()` dispatches purely on HTTP method and URL path — `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, `/shutdown` — with zero authentication check on the request: [1](#0-0) 

`process_event()` then simply reads the request body and JSON-deserializes it into the target event type without validating that it originated from the configured node: [3](#0-2) 

This is asymmetric with the node-side HTTP endpoints in the same protocol family (e.g. `/v3/block_proposal`), which do enforce an `Authorization` header check against `connection_options.auth_token` before accepting a submission: [4](#0-3) 

The `auth_token`/`auth_password` pairing documented for this feature only secures the signer→node direction (block proposal submission and `/v2/blocks?broadcast=1`); it is never checked by the signer's own event-receiving HTTP server for the node→signer direction: [5](#0-4) 

The reference deployment configuration binds this receiver to all interfaces by default: [6](#0-5) 

This breaks the intended equality "message came from the paired node" vs. "message came from any TCP client that reached the port" — the auth gate that exists elsewhere in the protocol (node-side `Authorization` check) simply does not exist on this leg of the transport, so it fails open.

### Impact Explanation
An attacker with network reachability to the signer's bound port can inject forged `StackerDBChunksEvent`/`BlockValidateResponse`/burn-block/new-block events directly into the signer's processing pipeline via unauthenticated POST requests, bypassing the intended node-signer trust boundary entirely. This is a remote, unauthenticated write into the signer's event stream — matching the "unauthenticated/unauthorized write to state" and "auth bypass" impact classes, since the entire authentication mechanism documented for node↔signer communication is one-directional and does not protect this HTTP surface at all.

### Likelihood Explanation
Likelihood is high wherever the signer's `endpoint` is reachable beyond localhost — which the shipped reference config (`endpoint = "0.0.0.0:30000"`) actively encourages. No credentials, TLS, or IP allowlisting are required to exploit; a bare `POST /stackerdb_chunks` (or the other recognized paths) with attacker-controlled JSON is sufficient, as demonstrated by the project's own test harness that talks to the receiver with plain unauthenticated HTTP. [7](#0-6) 

### Recommendation
Add authentication to the signer's event HTTP server: require the node to present a shared secret (e.g., the same `auth_token`/`auth_password` value) in an `Authorization` header on every event POST, and reject requests without a valid, constant-time-compared token before dispatching to `process_event`. Additionally, document/enforce binding the signer's `endpoint` to `127.0.0.1` or a private interface by default rather than `0.0.0.0`.

### Proof of Concept
1. Start a `stacks-signer` with the reference config (`endpoint = "0.0.0.0:30000"`).
2. From any host that can reach port 30000 (no credentials needed), send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"contract_id": "...", "modified_slots": [...]}
```
3. The request is accepted (200 OK acked) and, if the JSON deserializes, is forwarded into the signer's `main_loop` as a legitimate `SignerEvent`, as shown by `SignerEventReceiver::next_event` dispatching solely on URL/method with no credential check (`libsigner/src/events.rs:413-458`).

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

**File:** stackslib/src/net/api/tests/postblock_proposal.rs (L86-98)
```rust
    // missing authorization header
    let bad_request = http.handle_try_parse_request(
        &mut handler,
        &parsed_preamble.expect_request(),
        &bytes[offset..],
    );
    match bad_request {
        Err(crate::net::Error::Http(crate::net::http::Error::Http(err_code, message))) => {
            assert_eq!(err_code, 401);
            assert_eq!(message, "Unauthorized");
        }
        _ => panic!("expected error"),
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

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
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
