### Title
Unauthenticated, unbounded HTTP body read in `libsigner`'s event receiver enables remote memory-exhaustion DoS - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver` binds a plain `tiny_http`-based HTTP server (default sample config binds it to `0.0.0.0:30000`) to accept event POSTs "from the node." The handler `process_event` reads the entire request body into memory with `request.as_reader().read_to_string(&mut body)` before any deserialization, with no `Content-Length`/size cap and no authentication check on this listener. This mirrors the Litestar bug class: a body is read fully into RAM based on attacker-supplied framing (`Content-Length` or chunked transfer-encoding) without a bound, enabling a remote unauthenticated requester to exhaust the signer process's memory.

### Finding Description
`SignerEventReceiver::next_event` dispatches any inbound POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` straight into `process_event`: [1](#0-0) 

`process_event` then does: [2](#0-1) 

There is no size check on the body before `read_to_string`, and no authentication/authorization gate on this listener (the `auth_password`/`auth_token` mechanism documented in the sample config is for the *outgoing* signer→node RPC calls, not for this inbound event-receiver socket): [3](#0-2) 

The endpoint is expected to be bound where any remote host can reach it per the shipped sample config (`endpoint = "0.0.0.0:30000"`), and `bind()` just opens an `HttpServer` with no allow-list: [4](#0-3) 

By contrast, every other body-reading path in this codebase enforces an explicit upper bound before/while reading into memory: the P2P/HTTP `StacksHttp` protocol family enforces `MAX_MESSAGE_LEN`/`payload_len` checks in `stackslib/src/net/connection.rs` and `stackslib/src/net/httpcore.rs`, the chunked-transfer reader in `stacks-common/src/util/chunked_encoding.rs` caps `max_size`, and even the client-facing `libsigner::http::decode_http_body` bounds the chunked path via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`: [5](#0-4) 

`process_event`'s use of `read_to_string` with no cap and no gating breaks that same equality/invariant every other ingestion path in-scope enforces: "bytes accepted into memory" must be bounded by a protocol-level maximum before being read. Here it is not.

### Impact Explanation
Any remote, unauthenticated party that can reach the signer's event-receiver port (which per the shipped default configuration is bound to `0.0.0.0`, i.e., all interfaces) can issue a single `POST /stackerdb_chunks` (or any of the other three routes) with an oversized `Content-Length` or a chunked-encoded body of unbounded length. `tiny_http`'s reader will stream according to that attacker-controlled framing and `read_to_string` will keep growing the `String` buffer until memory is exhausted, crashing or hanging the signer process. Because signers are a required component of Nakamoto block production/attestation, an attacker capable of reaching this port on a meaningful fraction of signers could disrupt block signing availability. This fits "Critical – remote crash/unauthenticated DoS from few messages" against the signer process.

### Likelihood Explanation
No authentication, capability, or size check stands between an attacker and `read_to_string`; a single crafted TCP connection/HTTP POST is sufficient. The only mitigating factor is that operators may (or should) firewall this port to the node's IP only — but the shipped sample config explicitly binds to `0.0.0.0`, and nothing in the code enforces peer restriction, so misconfiguration (which the sample encourages) directly exposes the flaw. This is a very low-effort, single-request attack once the port is reachable.

### Recommendation
- Enforce a maximum body size before/while reading in `process_event` (e.g., wrap `request.as_reader()` in a bounded reader capped at a small, message-type-specific maximum such as `STACKERDB_MAX_CHUNK_SIZE`/`MAX_MESSAGE_LEN`, mirroring the bound already used in `libsigner::http::decode_http_body`).
- Reject requests whose `Content-Length` exceeds the bound before reading, and cap chunked-transfer reads the same way `HttpChunkedTransferReaderState` does elsewhere in this codebase.
- Consider requiring the event receiver to authenticate/allow-list the peer (e.g., verify the connection originates from the configured `node_host`, or require a shared secret) since currently any reachable host can post events.

### Proof of Concept
1. Start a signer configured per `sample/conf/signer/mainnet-signer-conf.toml` (`endpoint = "0.0.0.0:30000"`).
2. From a remote unprivileged host, open a TCP connection to port 30000 and send:
   `POST /stackerdb_chunks HTTP/1.1\r\nHost: <signer>\r\nContent-Length: 999999999999\r\nContent-Type: application/json\r\n\r\n`
   followed by an endless stream of bytes (or use `Transfer-Encoding: chunked` and never send a terminating 0-length chunk while continuously sending chunk data).
3. Observe `process_event`'s `request.as_reader().read_to_string(&mut body)` call [6](#0-5) 
   accumulate the streamed bytes into an unbounded `String`, growing the signer process's memory usage until it is killed by the OOM killer or the host becomes unresponsive.

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

**File:** libsigner/src/events.rs (L437-448)
```rust
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

**File:** libsigner/src/http.rs (L204-210)
```rust
    let body = if chunked {
        // chunked encoding
        let ptr = &mut buf;
        let mut fd = HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into());
        let mut decoded_body = vec![];
        fd.read_to_end(&mut decoded_body)?;
        decoded_body
```
