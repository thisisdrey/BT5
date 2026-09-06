### Title
Unbounded HTTP response buffering in `run_http_request`/`decode_http_body` allows memory-amplification before `StackerDBSession::get_latest_chunks` chunk-size check - (File: libsigner/src/session.rs, libsigner/src/http.rs)

### Summary
`StackerDBSession::get_latest_chunks` only rejects an oversized chunk *after* the full HTTP response body has already been read into memory. The underlying `run_http_request` reads the entire socket stream with `sock.read_to_end(&mut buf)` with no size cap at all, and for non-chunked bodies `decode_http_body` does an unconditional `buf.to_vec()` with no length check; only chunked-transfer-encoded bodies are bounded by `MAX_MESSAGE_LEN` via `HttpChunkedTransferReader`. The `body_bytes.len() > limit` check in `get_latest_chunks` (session.rs:243) is applied strictly after this allocation, so it cannot prevent the memory blow-up, only discard the oversized result afterward.

### Finding Description
The call chain is `get_latest_chunks` (`libsigner/src/session.rs:230-262`) → `rpc_request` (`session.rs:162-172`) → `run_http_request` (`libsigner/src/http.rs:221-262`) → `decode_http_body` (`http.rs:197-217`).

- `run_http_request` performs `sock.read_to_end(&mut buf)` (`http.rs:251`) with **no length limit whatsoever** — this will keep growing `buf` until the peer closes the connection or the process runs out of memory.
- `decode_http_body` only imposes `MAX_MESSAGE_LEN` when `Transfer-Encoding: chunked` is used (`http.rs:207`, via `HttpChunkedTransferReader::from_reader(ptr, MAX_MESSAGE_LEN.into())`). For a plain (non-chunked) body, it does `buf.to_vec()` (`http.rs:213`) with no cap.
- Only after the full body is decoded and returned does `get_latest_chunks` compare `body_bytes.len()` against `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` (`session.rs:243`), and if it exceeds the limit it simply drops the data (`None`) — the large allocation has already happened and cannot be "un-spent."

So the equality the question describes is real and confirmed: the check at `session.rs:243` executes strictly after the full response body is already materialized in memory, and for unchunked responses that allocation is not even bounded by `MAX_MESSAGE_LEN` — it is fully attacker-controlled up to whatever the peer chooses to send before closing the connection.

### Impact Explanation
Any endpoint that a `StackerDBSession` is configured to query (i.e., the node/RPC host the signer talks to) can return an arbitrarily large HTTP body for a single `GET` chunk request. This forces the signer's `libsigner` client process to allocate a buffer of that size on every fetch, well beyond the intended `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` limits, and for non-chunked bodies with no upper bound at all. Repeated or single very-large responses can exhaust memory on the signer process, causing a crash/DoS.

### Likelihood Explanation
This requires the attacker to control (or MITM) the specific RPC endpoint that the signer's `StackerDBSession.host` is configured to talk to. In typical deployments this is the signer operator's own paired Stacks node, not an arbitrary untrusted third party — exploiting it via MITM would need control of that specific network path, which is a stronger precondition than the generic "any remote peer" attacker model used elsewhere in this audit. If that host is compromised, malicious, or successfully MITM'd, exploitation is a single crafted HTTP response with no authentication or secret required (it's an outbound GET with no auth header). No chunk signature or slot-owner key is needed since the check that fails is a pure length check performed too late.

### Recommendation
Bound `sock.read_to_end` and `decode_http_body`/`run_http_request` by a caller-supplied maximum size (e.g., the `SIGNERS_STACKERDB_CHUNK_SIZE`/`STACKERDB_MAX_CHUNK_SIZE` limit, or at minimum `MAX_MESSAGE_LEN`) applied uniformly to both chunked and non-chunked bodies, aborting/truncating the read as soon as the limit is exceeded rather than after full buffering. Ideally, honor `Content-Length` and reject/stream-cap reads that exceed the expected chunk-size limit before allocating.

### Proof of Concept
Rust test in `libsigner::session` (extending the existing `tests` module in `session.rs`) using a mock `TcpListener`:
1. Spawn a listener thread that, upon accepting a connection, writes `"HTTP/1.1 200 OK\r\nContent-Length: <N>\r\n\r\n"` followed by `N` bytes where `N = STACKERDB_MAX_CHUNK_SIZE + 1_000_000` (no `Transfer-Encoding: chunked` header).
2. Call `StackerDBSession::get_latest_chunks(&[0])` against this listener.
3. Instrument/assert that `run_http_request`'s `buf` (or a wrapped allocator) grows to `N` bytes before `get_latest_chunks` discards the value — i.e., assert peak allocation ≈ `N`, not capped near `STACKERDB_MAX_CHUNK_SIZE`, demonstrating the check at `session.rs:243` occurs post-allocation. The returned `Option<Vec<u8>>` will correctly be `None`, but the memory spike already occurred inside `run_http_request`/`decode_http_body` (`http.rs:251,213`).