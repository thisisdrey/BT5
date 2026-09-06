### Title
Signer event-listener HTTP endpoint accepts unauthenticated pushes from any remote host - ([File: libsigner/src/events.rs])

### Summary
The `stacks-signer` process runs a local HTTP server (`SignerEventReceiver`) that is meant to receive event pushes only from its paired `stacks-node`. Unlike the node's own RPC (which is gated by `[connection_options] auth_token`, checked in `postblock_v3.rs`), the signer's inbound listener performs **no authentication check whatsoever** on `POST /stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` requests, and sample configs bind it to `0.0.0.0`.

### Finding Description
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` dispatches any inbound POST purely by URL path and directly deserializes the body into the corresponding event type, with zero credential/token verification: [1](#0-0) 

`process_event` similarly performs no auth check — it reads the body, JSON-decodes it, and hands it to the signer runloop as a trusted `SignerEvent`: [2](#0-1) 

A grep across `libsigner/src/events.rs` for any auth/token/password check confirms none exists — the outbound `auth_password`/`auth_token` mechanism documented in `docs/signing.md` and the sample configs only protects the *node's* RPC (miner→node block-proposal submissions via `postblock_v3.rs`'s `Authorization` header check), not the *signer's* inbound event listener: [3](#0-2) 

The sample configuration explicitly binds this unauthenticated listener to all interfaces: [4](#0-3) 

This breaks the intended equality "only the paired node may push signer events" vs. "any host that can reach the port may push signer events." It is a direct network-facing analog of the reported CVE's bug class: a control-plane action (feeding state into the signer) that should require an authenticated/legitimate origin is instead reachable by an unauthenticated third party, purely because the transport layer performs no origin verification (comparable to Jenkins SSH plugin accepting attacker-controlled parameters without CSRF/auth protection on a sensitive endpoint).

### Impact Explanation
Any remote host that can reach the configured `endpoint` port (`0.0.0.0:30000` per the shipped sample configs) can POST arbitrary JSON to `/stackerdb_chunks`, `/proposal_response`, or `/new_burn_block` and have it accepted into the signer's event stream as though it came from the legitimate paired node. Individual event payload types (e.g., `StackerDBChunkData`) carry their own signature fields that are validated further downstream, which limits — but does not eliminate — impact for that specific event type; however, `BurnBlockEvent` and `BlockValidateResponse` (`/new_burn_block`, `/proposal_response`) carry no such downstream signer/cryptographic verification against the injected transport, meaning forged burn-block or proposal-validation-response data can reach the signer's event channel from the network. This matches the "unauthenticated/unauthorized write to state" and "network-wide propagation of forged data" severity bar, since the entry point through which trusted node state normally flows into the signer has no gate at all.

### Likelihood Explanation
High: the sample/reference configs (`mainnet-signer-conf.toml`, and node-side `mainnet-signer.toml`) bind the listener to `0.0.0.0`, and nothing in `libsigner/src/events.rs` prevents connections from arbitrary sources. No credentials, tokens, or TLS/mTLS are required to reach the endpoint — only network reachability to the configured port.

### Recommendation
- Require the signer's HTTP event listener to validate a shared secret/token (mirroring the node's `auth_token`) on every inbound request in `SignerEventReceiver::next_event`/`process_event`, rejecting requests without it before deserializing/dispatching.
- Default the sample/reference configs to bind the listener to `127.0.0.1` rather than `0.0.0.0`, and add a startup warning if bound to a non-loopback address without an auth token.
- Add integration tests asserting that unauthenticated requests to `/stackerdb_chunks`, `/proposal_response`, and `/new_burn_block` are rejected.

### Proof of Concept
1. Deploy `stacks-signer` with the shipped `mainnet-signer-conf.toml` (`endpoint = "0.0.0.0:30000"`).
2. From a separate host with network access to port 30000, send:
```
POST /new_burn_block HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{ ...forged BurnBlockEvent JSON... }
```
3. Observe (as shown by the existing test harness pattern in `libsigner/src/tests/mod.rs`, which sends unauthenticated POSTs over a raw `TcpStream`) that the signer's `next_event()` accepts and forwards the payload into its runloop without ever checking the request's origin or any credential, exactly as it does for legitimate node traffic. [5](#0-4)

### Citations

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

**File:** libsigner/src/tests/mod.rs (L120-147)
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
    });
```
