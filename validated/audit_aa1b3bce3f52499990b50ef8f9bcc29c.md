### Title
Unauthenticated Signer Event-Receiver Accepts Forged Push Events (StackerDB chunks, block-validation results, burn blocks) - (File: `libsigner/src/events.rs`)

### Summary
The signer-side HTTP event receiver (`SignerEventReceiver::next_event`) that the `stacks-node` uses to push `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block` and `/new_block` events into the signer's decision runloop performs **no authentication whatsoever** on incoming requests. It only inspects the HTTP method and URL path before deserializing the body and forwarding it straight into the signer runloop channel. Any host that can reach the configured listener socket can POST forged events and have them treated as if they came from the trusted node.

### Finding Description
`next_event()` dispatches purely on `request.url()` and `request.method()`: [1](#0-0) 

None of the branches (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) check any `Authorization` header, token, or peer identity before calling `process_event`, which simply reads the body and deserializes it: [2](#0-1) 

The intended protection is documented as a shared secret (`auth_token` on the node / `auth_password` on the signer) that operators are told must match: [3](#0-2) 

However, this token is only enforced on the **node's own RPC server** (`StacksHttp::new` wires `conn_opts.auth_token` into the node's request handling), not on the signer's event receiver: [4](#0-3) 

And the outgoing push from the node's event dispatcher (`make_http_request`) never attaches any `Authorization`/token header — it only sets `Connection: close`: [5](#0-4) 

So the "must match" `auth_token`/`auth_password` pairing described in the docs is not actually wired into this push path at all: the receiver in `libsigner/src/events.rs` fails open unconditionally. Sample configs even show the signer binding to all interfaces (`endpoint = "0.0.0.0:30000"`), which is explicitly recommended in `docs/signing.md`, making the unauthenticated listener remotely reachable.

This breaks the equality "event pushed by the trusted node" vs. "event accepted from any network peer" — the receiver treats any well-formed POST to a recognized path as authentic, exactly mirroring the PraisonAI bug class of an unauthenticated stream/endpoint exposing (here: also accepting into) all agent/signer activity.

### Impact Explanation
An unauthenticated remote attacker who can reach the signer's event-receiver port can:
- Inject a forged `BlockValidateResponse` (`/proposal_response`) — e.g., a fabricated "Ok" or "Reject" validation result — directly into the signer's decision pipeline without the node ever validating the corresponding block.
- Inject forged `BurnBlockEvent` (`/new_burn_block`) or `StacksBlockEvent` (`/new_block`) data that the signer runloop consumes as ground truth.
- Inject fabricated `StackerDBChunksEvent` payloads.

Because these events are forwarded to `out_channels` (i.e., directly into the signer's runloop) with zero authentication, this is an unauthenticated write of forged data into signer state — matching the "Critical: unauthenticated/unauthorized write to state... network-wide propagation of forged data" bucket, and is a stronger analog than the original disclosure-only PraisonAI bug.

### Likelihood Explanation
Requires the attacker to reach the TCP port the signer binds its event receiver on. Operators are explicitly guided by the shipped sample configs (`sample/conf/mainnet-signer-conf.toml`, `docs/signing.md`) to bind this on `0.0.0.0`, and the documented `auth_token`/`auth_password` "must match" guidance gives operators false confidence that this channel is authenticated, when in fact the code path enforces nothing. Any operator following the documented deployment pattern is exposed to any network attacker able to reach that port.

### Recommendation
Add authentication to `SignerEventReceiver::next_event` (e.g., require and verify an `Authorization`/token header matching the configured `auth_password`/`auth_token` before processing `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), and have `stacks-node`'s event dispatcher (`stacks-node/src/event_dispatcher/worker.rs`) attach that token when POSTing events. Alternatively, strongly bind this listener to loopback only and document that non-loopback binding is unsafe without additional network-layer authentication.

### Proof of Concept
1. Deploy a signer using the sample config with `endpoint = "0.0.0.0:30000"` (as documented in `docs/signing.md` / `sample/conf/signer/mainnet-signer-conf.toml`).
2. From a remote unauthenticated host, send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"result":"Ok", "block": "...", "cost": {...}, "size": 100}
```
3. `SignerEventReceiver::next_event` accepts the request (no auth check), deserializes it via `process_event::<T, BlockValidateResponse>`, and forwards it into the signer runloop as if the node had validated the block — with no verification that the request actually originated from the paired `stacks-node`.

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

**File:** docs/signing.md (L24-59)
```markdown

```toml
[node]
stacker = true

[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```

### 2. Configure the Signer

Use [`mainnet-signer-conf.toml`](../sample/conf/signer/mainnet-signer-conf.toml) as a starting point.
Key settings:

```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```

### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
```

**File:** stackslib/src/net/httpcore.rs (L1020-1044)
```rust
impl StacksHttp {
    /// Create an HTTP protocol state machine that handles the built-in RPC API.
    /// Used for building the RPC server
    pub fn new(peer_addr: SocketAddr, conn_opts: &ConnectionOptions) -> StacksHttp {
        let mut http = StacksHttp {
            peer_addr,
            body_start: None,
            num_preamble_bytes: 0,
            last_four_preamble_bytes: [0u8; 4],
            reply: None,
            chunk_size: 8192,
            request_handler_index: None,
            request_handlers: vec![],
            maximum_call_argument_size: conn_opts.maximum_call_argument_size,
            read_only_call_limit: conn_opts.read_only_call_limit.clone(),
            auth_token: conn_opts.auth_token.clone(),
            allow_arbitrary_response: false,
            read_only_max_execution_time: Duration::from_secs(
                conn_opts.read_only_max_execution_time_secs,
            ),
            read_only_call_max_mem_bytes: conn_opts.read_only_call_max_mem_bytes,
        };
        http.register_rpc_methods();
        http
    }
```

**File:** stacks-node/src/event_dispatcher/worker.rs (L325-355)
```rust
    fn make_http_request(
        data: &EventRequestData,
        disable_retries: bool,
    ) -> Result<(), EventDispatcherError> {
        debug!(
            "Event dispatcher: Sending payload"; "url" => &data.url, "bytes" => data.payload_bytes.len()
        );

        let url = Url::parse(&data.url)
            .unwrap_or_else(|_| panic!("Event dispatcher: unable to parse {} as a URL", data.url));

        let host = url.host_str().expect("Invalid URL: missing host");
        let port = url.port_or_known_default().unwrap_or(80);
        let peerhost: PeerHost = format!("{host}:{port}")
            .parse()
            .unwrap_or(PeerHost::DNS(host.to_string(), port));

        let mut backoff = Duration::from_millis(100);
        let mut attempts: i32 = 0;
        // Cap the backoff at 3x the timeout
        let max_backoff = data.timeout.saturating_mul(3);

        loop {
            let mut request = StacksHttpRequest::new_for_peer(
                peerhost.clone(),
                "POST".into(),
                url.path().into(),
                HttpRequestContents::new().payload_json_bytes(Arc::clone(&data.payload_bytes)),
            )
            .unwrap_or_else(|_| panic!("FATAL: failed to encode infallible data as HTTP request"));
            request.add_header("Connection".into(), "close".into());
```
