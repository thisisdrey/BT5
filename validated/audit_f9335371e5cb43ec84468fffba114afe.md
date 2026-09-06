### Title
Unauthenticated single request with unbounded body read hangs `SignerEventReceiver`'s single-threaded event loop - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::bind` starts a `tiny_http::HttpServer` with no read timeout and no authentication of the connecting peer. `process_event` reads the entire request body via `request.as_reader().read_to_string(&mut body)` before any validation, and because `next_event` processes requests serially in a single loop, a peer that opens a connection, declares a large `Content-Length`, and then stalls or trickles bytes can block the receiver thread indefinitely, starving all subsequent legitimate events (StackerDB chunks, block proposals, burn blocks).

### Finding Description
`SignerEventReceiver::bind` (`libsigner/src/events.rs:404-408`) simply calls `HttpServer::http(listener)` with no configured read/idle timeout. `next_event` (`libsigner/src/events.rs:413-459`) calls `http_server.recv()` to accept one request at a time and then dispatches it synchronously to `process_event` on the *same* thread — there is no worker pool or per-connection timeout guarding this call chain.

`process_event` (`libsigner/src/events.rs:519-542`) performs:
```
let mut body = String::new();
if let Err(e) = request.as_reader().read_to_string(&mut body) { ... }
```
at line 526, with no length check against any cap (no `MAX_MESSAGE_LEN` or similar), no secret/auth check, and no explicit socket read timeout before this blocking call.

An attacker with mere TCP reachability to the bound port can open a socket and send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <addr>
Content-Length: 4294967295
Content-Type: application/json

<a few bytes, then stop or trickle>
```
`tiny_http`'s body reader will only signal EOF once `Content-Length` bytes have been consumed (or the connection is dropped), so `read_to_string` will block on the underlying socket read waiting for more bytes that never arrive. Because the receiver's `next_event` loop is not multi-threaded/concurrent — each call to `recv()` and its subsequent `process_event` happens serially — the entire signer's event pipeline stalls for as long as the attacker holds the connection open and withholds bytes, deferring or dropping all StackerDB chunk/block-proposal/burn-block events the node tries to deliver in the meantime.

There is no authentication gate before this read: `process_event` does not check any secret, HMAC, or signature on the raw HTTP request before attempting to consume the body, so the fault is reachable by any TCP-capable party, not just the local node.

### Impact Explanation
A single crafted request can hang the signer's sole event-processing thread indefinitely, which can delay or drop delivery of legitimate signer events (block proposals for signing, StackerDB chunk updates, burn block notifications) for as long as the malicious connection is held. This is a low-cost, repeatable, unauthenticated denial-of-service against the signer's runloop input path, matching the "Critical – remote crash/unauthenticated DoS from few messages" category, assuming the bound address is reachable by the attacker (deployment-dependent; many deployments bind this to `127.0.0.1`, which would make it non-remote).

### Likelihood Explanation
Requires only that `SignerEventReceiver::bind` be configured on an address reachable by the attacker (this is an operator/deployment choice, not enforced to be localhost-only in this code). No secret, peer identity, or slot ownership is required — a bare TCP connection with a crafted HTTP request suffices, and the attack is trivially repeatable at negligible cost per attempt.

### Recommendation
- Set an explicit read/idle timeout on the `tiny_http` server/socket (e.g., `HttpServer::http` config or wrapping the stream with a timeout) so that stalled request bodies are aborted.
- Enforce an explicit maximum body size before attempting to read (reject or truncate reads when `Content-Length` exceeds a `MAX_MESSAGE_LEN`-style bound, and use a bounded `take()` reader rather than unconditional `read_to_string`).
- Process event connections concurrently (or with a watchdog) so that one stalled connection cannot block delivery of legitimate events.

### Proof of Concept
Rust integration test in `libsigner::events` (or a `TcpStream`-based test):
1. Start a `SignerEventReceiver` bound to `127.0.0.1:0`, spawn `next_event` on a thread.
2. From a separate `TcpStream`, send `POST /stackerdb_chunks HTTP/1.1\r\nContent-Length: 4294967295\r\nContent-Type: application/json\r\n\r\n` followed by a handful of bytes, then stop writing (do not close the socket).
3. Assert that `next_event` on the receiver thread does not return within a bounded time window (e.g., 2 seconds), demonstrating the read at `libsigner/src/events.rs:526` blocks indefinitely rather than timing out or enforcing a length cap.
4. Additionally attempt to open a second legitimate connection to `/status` from another thread while the first connection is stalled, and show it is not serviced until the malicious connection is closed/times out, proving pipeline starvation.