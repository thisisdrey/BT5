### Title
Unbounded HTTP body read in `process_event` allows single-request memory-exhaustion DoS against the signer's event receiver - (File: libsigner/src/events.rs)

### Summary
`process_event` in `libsigner/src/events.rs` reads the entire HTTP request body into a `String` via `request.as_reader().read_to_string(&mut body)` before any size validation or JSON parsing occurs. Unlike the stackslib node's HTTP handlers (`postblock.rs`, `postmicroblock.rs`, `posttransaction.rs`, `txsimulate.rs`), which all check `preamble.get_content_length() > MAX_PAYLOAD_LEN` before touching the body, there is no equivalent cap anywhere in `SignerEventReceiver`'s request path.

### Finding Description
The claimed equality — "bytes buffered for one event == a bounded, sane maximum" — is false. In `process_event` (`libsigner/src/events.rs:519-542`):
```rust
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
...
let json_event: E = serde_json::from_slice(body.as_bytes())...
```
The `tiny_http::Request::as_reader()` returns a reader that will read up to whatever `Content-Length` the client declares, and `read_to_string` will grow the `String` buffer until that many bytes are consumed (or the connection is closed), with no cap. This is called for `/stackerdb_chunks`, `/proposal_response`, and `/new_burn_block` (`libsigner/src/events.rs:437-447`). By contrast, this same repo enforces `MAX_PAYLOAD_LEN` checks against `Content-Length` before reading bodies in the node's stackslib HTTP handlers (e.g. `stackslib/src/net/api/postblock.rs:95-99`, `postmicroblock.rs:92-96`), demonstrating the pattern exists elsewhere but is absent here. `BoundReader` (`stacks-common/src/util/retry.rs:76-113`) and `HttpChunkedTransferReader` with `max_size` (`stacks-common/src/util/chunked_encoding.rs:73-104`) are examples of bounded-reader utilities already present in the codebase, but neither is applied to the signer's `tiny_http`-based receiver.

The `SignerEventReceiver` HTTP port is the operator-configured `[[events_observer]] endpoint` / signer `endpoint` (default sample configs show `0.0.0.0:30000`, see `sample/conf/signer/mainnet-signer-conf.toml:39` and `sample/conf/mainnet-signer.toml:26-28`). There is no authentication whatsoever on this receiver — it processes any HTTP POST to a recognized path without a shared secret; the `auth_token`/`auth_password` in these configs is used for a different channel (the node's block-proposal RPC), not this listener. `stacks-signer/src/lib.rs:125-132` explicitly warns operators that exposing this endpoint externally "could potentially expose sensitive data or functionalities to security risks," confirming this is a known-risky, potentially internet-reachable listener with no built-in access control.

### Impact Explanation
A single POST request with an oversized `Content-Length` and matching body causes the signer process to buffer the entire body in memory in one `String` before any JSON validation, size capping, or field parsing. This is a single-message, unauthenticated memory-exhaustion DoS against the signer process (not a distributed/volumetric attack — one connection, one request). If the operator's `endpoint` is bound to a routable address (as shown in the sample production configs), any remote TCP peer that can reach that port can trigger it, causing the signer's event-processing thread (and potentially the whole process via OOM) to become unresponsive, preventing the signer from participating in block signing.

### Likelihood Explanation
Preconditions: the operator must have the signer's `endpoint` reachable from the attacker's network (the sample mainnet/testnet configs default to `0.0.0.0:30000`, and the codebase itself documents this listener as commonly exposed and warns about exposing it externally). No secret, peer registration, or StackerDB slot ownership is needed — the request is accepted before any signature/authentication check. Attacker cost is roughly 1:1 with the memory it forces the signer to allocate (must upload as many bytes as it wants buffered), which is a real but bandwidth-bound cost; still, a single request (not a flood) exhausts memory proportional to attacker upload, with no code-level cap.

### Recommendation
Enforce a maximum body size in `process_event` before reading: check `request.headers()` for `Content-Length` against a bounded constant (e.g., mirror `MAX_PAYLOAD_LEN` used elsewhere in this repo, or a signer-specific cap sized to the largest legitimate `StackerDBChunksEvent`/`BlockValidateResponse`/`BurnBlockEvent` payload), and reject/close the connection if it's exceeded. Additionally, use a bounded reader (e.g., wrap `request.as_reader()` with something like `BoundReader` from `stacks-common/src/util/retry.rs`) to cap actual bytes read even if `Content-Length` is absent or understated, so chunked/streaming bodies can't bypass the header check.

### Proof of Concept
```rust
// In libsigner/src/tests/mod.rs style, similar to test_simple_signer / test_status_endpoint:
// 1. Spawn SignerEventReceiver on 127.0.0.1:<port> via Signer::spawn.
// 2. From a separate thread, open a TcpStream to the bound endpoint and send:
//    "POST /stackerdb_chunks HTTP/1.1\r\nHost: ...\r\nConnection: close\r\n
//     Content-Type: application/json\r\nContent-Length: <huge_N>\r\n\r\n"
//    followed by streaming <huge_N> bytes of arbitrary filler (e.g. loop writing chunks of zero
//    bytes or garbage) without ever forming valid JSON.
// 3. Instrument the test process (e.g., run under a memory-limited cgroup/ulimit, or track
//    allocator stats via a custom global allocator) and assert that RSS/heap usage grows
//    monotonically with N before serde_json::from_slice ever runs (add a println!/breakpoint
//    right before the serde_json::from_slice call in process_event to confirm body.len() == N
//    was fully buffered).
// 4. Expected: with a small memory ulimit and large N, the process aborts (OOM) or memory usage
//    exceeds any sane bound, proving there's no size cap between accepting the connection and
//    the serde_json parse step in libsigner/src/events.rs process_event.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** libsigner/src/events.rs (L437-447)
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

**File:** stackslib/src/net/api/postblock.rs (L82-99)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-zero-length body for PostBlock".to_string(),
            ));
        }

        if preamble.get_content_length() > MAX_PAYLOAD_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: PostBlock body is too big".to_string(),
            ));
        }
```

**File:** stacks-common/src/util/retry.rs (L76-113)
```rust
/// A Read that will only read up to a given number of bytes before EOF'ing.
pub struct BoundReader<'a, R: Read> {
    fd: &'a mut R,
    max_len: u64,
    read_so_far: u64,
}

impl<'a, R: Read> BoundReader<'a, R> {
    pub fn from_reader(reader: &'a mut R, max_len: u64) -> BoundReader<'a, R> {
        BoundReader {
            fd: reader,
            max_len,
            read_so_far: 0,
        }
    }

    pub fn num_read(&self) -> u64 {
        self.read_so_far
    }
}

impl<R: Read> Read for BoundReader<'_, R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let intended_read = self
            .read_so_far
            .checked_add(buf.len() as u64)
            .ok_or_else(|| io::Error::other("Read would overflow u64".to_string()))?;
        let max_read = if intended_read > self.max_len {
            self.max_len - self.read_so_far
        } else {
            buf.len() as u64
        };

        let nr = self.fd.read(&mut buf[0..(max_read as usize)])?;
        self.read_so_far += nr as u64;
        Ok(nr)
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
