### Title
Unbounded HTTP response read enables remote memory-exhaustion "return bomb" against StackerDB clients - (File: libsigner/src/http.rs)

### Summary
`run_http_request` in `libsigner/src/http.rs`, the transport used by `StackerDBSession` (`libsigner/src/session.rs`) to talk to a StackerDB HTTP replica, reads an entire HTTP response into memory with no upper bound before any content-length or body-size check is applied, unlike every other HTTP client decode path in `stackslib/src/net/api/*` which caps response bytes via `parse_bytes`/`parse_raw_bytes` against `MAX_MESSAGE_LEN`/`MAX_PAYLOAD_LEN`/`STACKERDB_MAX_CHUNK_SIZE`.

### Finding Description
`run_http_request` writes the request and then does: [1](#0-0) 

`sock.read_to_end(&mut buf)` has no size cap: a malicious or misbehaving StackerDB replica endpoint can respond with an arbitrarily large body (or an arbitrarily long stream that never closes cleanly but keeps sending data), causing the client to allocate memory proportional to whatever the server sends before any length/limit check occurs. Only after the entire buffer has been read does the code call `decode_http_response`/`decode_http_body`, and even then, the length cap (`MAX_MESSAGE_LEN`) is only applied in the chunked-transfer-encoding branch of `decode_http_body`: [2](#0-1) 
For a non-chunked response, the code takes the already-fully-buffered bytes as-is (`buf.to_vec()`), meaning the unbounded allocation already happened in `read_to_end` regardless of encoding.

This is used by `StackerDBSession::rpc_request`, which backs `list_chunks`, `get_chunks`, `get_latest_chunks`, and `put_chunk`: [3](#0-2) 

By contrast, every equivalent client-side decode path within `stackslib/src/net/api/*` (e.g. `getblock.rs`, `getblock_v3.rs`, `gettenure.rs`, `getstackerdbchunk.rs`) explicitly caps the number of bytes accepted from an HTTP response body via `parse_bytes`/`parse_raw_bytes` against a fixed maximum (`MAX_MESSAGE_LEN`, `MAX_PAYLOAD_LEN`, or `STACKERDB_MAX_CHUNK_SIZE`) before allocating a `Vec<u8>` for the payload: [4](#0-3) [5](#0-4) 

The `libsigner` client breaks this equality: every other network-facing byte-length gate in this codebase enforces "bytes read ≤ declared/expected size" before allocation, but `run_http_request` allocates first and checks size second (and only for chunked bodies).

### Impact Explanation
A StackerDB replica host that a signer process is configured to query (or that becomes byzantine/compromised without any need for the node's private key or admin privilege over the signer itself) can return a multi-gigabyte HTTP body to any of `list_chunks`/`get_chunks`/`get_latest_chunks`/`put_chunk`. This forces the signer's `libsigner` client to allocate unbounded memory in `read_to_end`, leading to memory exhaustion and process crash/OOM — an unauthenticated remote DoS against the signer process reachable with a handful of messages, consistent with the report's return-bomb bug class (unbounded response size causing resource exhaustion on the caller) mapped onto the client side of the StackerDB RPC protocol rather than the low-level-call return value in the original Solidity report.

### Likelihood Explanation
Likelihood is moderate: it requires the signer (or any user of `StackerDBSession`) to be pointed at, or have connectivity redirected to, a hostile HTTP endpoint speaking the StackerDB chunk protocol. This is plausible in deployments where a node operator's signer talks to a node that becomes compromised, or where `hint_replicas`/user-supplied host configuration points at an untrusted or attacker-influenced peer. No cryptographic signature or private key is required for the attacking side — it only needs to control the TCP responder.

### Recommendation
Apply the same bound used elsewhere in the codebase (`MAX_MESSAGE_LEN` / `STACKERDB_MAX_CHUNK_SIZE`) to `run_http_request` before or during the read: use a length-limited reader (e.g., `Read::take(limit)` combined with the parsed `Content-Length` header) instead of `sock.read_to_end(&mut buf)`, and enforce the cap for both chunked and non-chunked bodies inside `decode_http_body`, mirroring `parse_raw_bytes`'s max-length enforcement in `stackslib/src/net/http/common.rs`.

### Proof of Concept
1. Stand up a TCP listener that accepts a `StackerDBSession` request (e.g. `GET /v2/stackerdb/.../metadata`), replies with a valid `HTTP/1.1 200 OK` status line and headers (no `Content-Length`, or `Content-Length` set low but actual body far larger), and then streams several GB of arbitrary bytes without closing the connection cleanly (or closes only after fully sending the huge payload).
2. Have a `StackerDBSession` (as used by `list_chunks`/`get_chunks`/`get_latest_chunks`/`put_chunk`) connect to this listener.
3. Observe that `run_http_request`'s `sock.read_to_end(&mut buf)` call at `libsigner/src/http.rs:251` buffers the entire multi-GB payload into memory before `decode_http_body` gets a chance to apply any size check, causing uncontrolled memory growth and eventual OOM/crash of the signer process.

Note: I was unable to fully trace, within the available index, every call site that constructs a `StackerDBSession` with an externally-influenced or untrusted `host` value (the `grep_search` for `StackerDBSession::new` construction sites errored out due to a regex issue and could not be re-run in this session), so the exact deployment conditions under which the `host` becomes attacker-influenced (versus always being the signer operator's own trusted node) could not be fully confirmed from the indexed code alone.

### Citations

**File:** libsigner/src/http.rs (L196-217)
```rust
/// Decode an HTTP body, given the headers.
pub fn decode_http_body(headers: &HashMap<String, String>, mut buf: &[u8]) -> io::Result<Vec<u8>> {
    let chunked = if let Some(val) = headers.get("transfer-encoding") {
        val == "chunked"
    } else {
        false
    };

    let body = if chunked {
        // chunked encoding
        let ptr = &mut buf;
        let mut fd = HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into());
        let mut decoded_body = vec![];
        fd.read_to_end(&mut decoded_body)?;
        decoded_body
    } else {
        // body is just as-is
        buf.to_vec()
    };

    Ok(body)
}
```

**File:** libsigner/src/http.rs (L246-253)
```rust
    sock.write_all(req_txt.as_bytes())?;
    sock.write_all(payload)?;

    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
```

**File:** libsigner/src/session.rs (L160-273)
```rust
    /// send an HTTP RPC request and receive a reply.
    /// Return the HTTP reply, decoded if it was chunked
    fn rpc_request(
        &mut self,
        verb: &str,
        path: &str,
        content_type: Option<&str>,
        payload: &[u8],
    ) -> Result<Vec<u8>, RPCError> {
        self.with_socket(|session, sock| {
            run_http_request(sock, &session.host, verb, path, content_type, payload)
        })?
    }
}

impl SignerSession for StackerDBSession {
    /// connect to the replica
    fn connect(
        &mut self,
        host: String,
        stackerdb_contract_id: QualifiedContractIdentifier,
    ) -> Result<(), RPCError> {
        self.host = host;
        self.stackerdb_contract_id = stackerdb_contract_id;
        self.connect_or_reconnect()
    }

    /// query the replica for a list of chunks
    fn list_chunks(&mut self) -> Result<Vec<SlotMetadata>, RPCError> {
        let bytes = self.rpc_request(
            "GET",
            &stackerdb_get_metadata_path(self.stackerdb_contract_id.clone()),
            None,
            &[],
        )?;
        let metadata: Vec<SlotMetadata> = serde_json::from_slice(&bytes)
            .map_err(|e| RPCError::Deserialize(format!("{:?}", e)))?;
        Ok(metadata)
    }

    /// query the replica for zero or more chunks
    fn get_chunks(
        &mut self,
        slots_and_versions: &[(u32, u32)],
    ) -> Result<Vec<Option<Vec<u8>>>, RPCError> {
        let mut payloads = vec![];
        for (slot_id, slot_version) in slots_and_versions.iter() {
            let path = stackerdb_get_chunk_path(
                self.stackerdb_contract_id.clone(),
                *slot_id,
                Some(*slot_version),
            );
            let chunk = match self.rpc_request("GET", &path, None, &[]) {
                Ok(body_bytes) => Some(body_bytes),
                Err(RPCError::HttpError(code)) => {
                    if code != 404 {
                        return Err(RPCError::HttpError(code));
                    }
                    None
                }
                Err(e) => {
                    return Err(e);
                }
            };
            payloads.push(chunk);
        }
        Ok(payloads)
    }

    /// query the replica for zero or more latest chunks
    fn get_latest_chunks(&mut self, slot_ids: &[u32]) -> Result<Vec<Option<Vec<u8>>>, RPCError> {
        let mut payloads = vec![];
        let limit = if self.stackerdb_contract_id.name.starts_with("signer") {
            SIGNERS_STACKERDB_CHUNK_SIZE
        } else {
            usize::try_from(STACKERDB_MAX_CHUNK_SIZE)
                .expect("infallible: StackerDB chunk size exceeds usize::MAX")
        };
        for slot_id in slot_ids.iter() {
            let path = stackerdb_get_chunk_path(self.stackerdb_contract_id.clone(), *slot_id, None);
            let chunk = match self.rpc_request("GET", &path, None, &[]) {
                Ok(body_bytes) => {
                    // Verify that the chunk is not too large
                    if body_bytes.len() > limit {
                        None
                    } else {
                        Some(body_bytes)
                    }
                }
                Err(RPCError::HttpError(code)) => {
                    if code != 404 {
                        return Err(RPCError::HttpError(code));
                    }
                    None
                }
                Err(e) => {
                    return Err(e);
                }
            };
            payloads.push(chunk);
        }
        Ok(payloads)
    }

    /// upload a chunk
    fn put_chunk(&mut self, chunk: &StackerDBChunkData) -> Result<StackerDBChunkAckData, RPCError> {
        let body =
            serde_json::to_vec(chunk).map_err(|e| RPCError::Deserialize(format!("{e:?}")))?;
        let path = stackerdb_post_chunk_path(self.stackerdb_contract_id.clone());
        let resp_bytes = self.rpc_request("POST", &path, Some("application/json"), &body)?;
        let ack: StackerDBChunkAckData = serde_json::from_slice(&resp_bytes)
            .map_err(|e| RPCError::Deserialize(format!("{e:?}")))?;
        Ok(ack)
    }
```

**File:** stackslib/src/net/http/common.rs (L109-127)
```rust
/// Helper function to read a raw bytestream
pub fn parse_raw_bytes(
    preamble: &HttpResponsePreamble,
    body: &[u8],
    max_len: u64,
    expected_content_type: HttpContentType,
) -> Result<Vec<u8>, Error> {
    if preamble.content_type != expected_content_type {
        return Err(Error::DecodeError(format!(
            "Invalid content-type: expected {}",
            expected_content_type
        )));
    }
    let out_len = usize::try_from(max_len).unwrap().min(body.len());
    let out_bytes = body
        .get(..out_len)
        .ok_or_else(|| Error::DecodeError("Unexpected body size".into()))?;
    Ok(out_bytes.to_vec())
}
```

**File:** stackslib/src/net/api/getstackerdbchunk.rs (L192-204)
```rust
/// Decode the HTTP response
impl HttpResponse for RPCGetStackerDBChunkRequestHandler {
    /// Decode this response from a byte stream.  This is called by the client to decode this
    /// message
    fn try_parse_response(
        &self,
        preamble: &HttpResponsePreamble,
        body: &[u8],
    ) -> Result<HttpResponsePayload, Error> {
        let data: Vec<u8> = parse_bytes(preamble, body, STACKERDB_MAX_CHUNK_SIZE.into())?;
        Ok(HttpResponsePayload::Bytes(data))
    }
}
```
