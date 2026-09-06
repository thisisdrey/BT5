### Title
Unbounded body read + unbounded hex-decode allocation on `SignerEventReceiver` `/new_block` before any `StacksTransaction` size validation - (File: `libsigner/src/events.rs`)

### Summary
`process_event` in `libsigner/src/events.rs` reads an entire HTTP request body into a `String` with no length cap, then `deserialize_raw_tx_hex` hex-decodes each `raw_tx` field with `hex_bytes` into a freshly allocated `Vec<u8>` before `StacksTransaction::consensus_deserialize` ever runs. No `Content-Length` cap, no `MAX_MESSAGE_LEN`/`BoundReader` usage, and no authentication gate exist on this path, unlike the node's own HTTP layer (`stackslib/src/net/http/error.rs`, `stackslib/src/net/httpcore.rs`), which explicitly wraps bodies in `BoundReader::from_reader(..., MAX_MESSAGE_LEN)`.

### Finding Description
The claimed equality `bytes_allocated_for_decode == validated_max_tx_size` is broken. The path is:

1. `SignerEventReceiver::next_event` dispatches `POST /new_block` to `process_event::<T, StacksBlockEvent>(request)` [1](#0-0) .
2. `process_event` reads the whole body unbounded: `request.as_reader().read_to_string(&mut body)` [2](#0-1) , then hands it to `serde_json::from_slice::<StacksBlockEvent>` [3](#0-2) .
3. `StacksBlockEvent::transactions` is deserialized via `deserialize_raw_tx_hex`, which first deserializes a `Vec<NewBlockTransaction>` (each holding a `raw_tx: String`) and then calls `get_stacks_transaction()` on each [4](#0-3) .
4. `NewBlockTransaction::get_stacks_transaction` calls `hex_bytes(&self.raw_tx)`, which allocates a `Vec<u8>` proportional to the (attacker-controlled) hex string length, **before** `StacksTransaction::consensus_deserialize` is ever invoked [5](#0-4) .

There is no `Content-Length`/body-size cap, no `MAX_MESSAGE_LEN` bound reader, and no per-field size cap anywhere in this call chain — contrast with `stackslib/src/net/http/error.rs` and `stackslib/src/net/httpcore.rs`, which explicitly bound reads to `MAX_MESSAGE_LEN` via `BoundReader` [6](#0-5) . No such guard exists in `libsigner/src/events.rs`.

Additionally, the endpoint has no authentication: `next_event` dispatches purely based on the URL path with no secret/token check [7](#0-6) . The `auth_token`/`auth_password` pairing documented in the sample configs governs the signer's outbound calls *to the node's RPC*, not inbound POSTs to the signer's own event listener [8](#0-7) .

### Impact Explanation
A single crafted HTTP POST to `/new_block` with a multi-hundred-MB `raw_tx` hex string in `transactions[0]` forces the signer process to: (a) buffer the whole HTTP body into memory as a `String`, (b) buffer it again as parsed JSON, and (c) allocate a large `Vec<u8>` in `hex_bytes` — all before any transaction-size or structural validation occurs. This can exhaust the signer process's memory and crash it (or the host), with no signature, secret, or size check preventing it. It is trivially repeatable (send again), and it matches the **Critical: remote crash/unauthenticated DoS from few messages** category, since only one HTTP POST is required and the endpoint requires no authentication.

### Likelihood Explanation
Preconditions: the attacker must be able to reach the signer's event-listener TCP port. The maintained sample configs (`sample/conf/signer/mainnet-signer-conf.toml`, referenced by `docs/signing.md`) explicitly recommend `endpoint = "0.0.0.0:30000"` for the signer, i.e., bound on all interfaces [9](#0-8) . If an operator follows this default, the port is remotely reachable by any unprivileged network party, with no auth token required for POSTs to it. Attacker cost is a single HTTP request; the exploit is fully repeatable and requires no privileged role, no valid slot ownership, and no knowledge of any secret.

### Recommendation
- Bound the total request body size read in `process_event` (e.g., via a `Content-Length` check and/or a `BoundReader` capped at `MAX_MESSAGE_LEN`) before calling `read_to_string`/`serde_json::from_slice`.
- Impose an explicit maximum length on each `raw_tx` hex string (and/or the decoded byte length) in `deserialize_raw_tx_hex`/`NewBlockTransaction::get_stacks_transaction` prior to calling `hex_bytes`, matching `stacks-common`'s existing `MAX_MESSAGE_LEN` conventions used elsewhere in the codebase.
- Consider requiring the same `auth_token`/shared-secret validation on inbound event-receiver POSTs that is already used for the node's RPC endpoint, and/or default-document the event-receiver `endpoint` to bind to `127.0.0.1` rather than `0.0.0.0`.

### Proof of Concept
Rust test plan (using `libsigner`'s own `SignerEventReceiver`/`tiny_http` server):
```rust
// 1. Bind a SignerEventReceiver<SignerMessage> on 127.0.0.1:<port> via `bind()`.
// 2. Spawn a thread calling `next_event()` (which calls `process_event::<_, StacksBlockEvent>`).
// 3. From a separate "attacker" thread/process, open a TcpStream to the bound port and
//    stream an HTTP POST to /new_block with a JSON body:
//    { "index_block_hash": "0x..", "consensus_hash": "0x..", "block_hash": "0x..",
//      "block_height": 1,
//      "transactions": [ { "raw_tx": "0x" + "00".repeat(300_000_000) } ] }
//    i.e. a raw_tx hex string ~600MB long, sent with a matching Content-Length header.
// 4. Assert (fault site): before this PoC's fix, the process's RSS grows by ~300MB+
//    inside `hex_bytes(&self.raw_tx)` in `NewBlockTransaction::get_stacks_transaction`
//    (libsigner/src/events.rs:666) — i.e., allocation happens prior to any call to
//    `StacksTransaction::consensus_deserialize` (line 669) or any size-bound rejection.
// 5. Expected passing behavior for a fixed implementation: `process_event` should return
//    an `EventError` (e.g., "body too large" / "raw_tx too long") without ever allocating
//    a buffer larger than a fixed cap (e.g., MAX_MESSAGE_LEN), which the PoC should assert
//    via peak-memory measurement or by pre-emptively truncating/rejecting oversized bodies.
```
Note: I could not fully trace `hex_bytes`'s exact implementation (its definition site outside `stacks-common/src/util/hash.rs`'s visible portion was not resolved in this session), but its usage sites elsewhere in the codebase confirm it decodes a full hex string into a `Vec<u8>` with no size limit distinct from the input length itself, consistent with the described allocation-before-validation behavior.

### Citations

**File:** libsigner/src/events.rs (L436-458)
```rust
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

**File:** libsigner/src/events.rs (L524-533)
```rust
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
```

**File:** libsigner/src/events.rs (L536-537)
```rust
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;
```

**File:** libsigner/src/events.rs (L661-672)
```rust
impl NewBlockTransaction {
    pub fn get_stacks_transaction(&self) -> Result<Option<StacksTransaction>, CodecError> {
        if self.raw_tx == "00" {
            Ok(None)
        } else {
            let tx_bytes = hex_bytes(&self.raw_tx).map_err(|e| {
                CodecError::DeserializeError(format!("Failed to deserialize raw tx: {e}"))
            })?;
            let tx = StacksTransaction::consensus_deserialize(&mut &tx_bytes[..])?;
            Ok(Some(tx))
        }
    }
```

**File:** libsigner/src/events.rs (L676-688)
```rust
fn deserialize_raw_tx_hex<'de, D: serde::Deserializer<'de>>(
    d: D,
) -> Result<Vec<StacksTransaction>, D::Error> {
    let tx_objs: Vec<NewBlockTransaction> = serde::Deserialize::deserialize(d)?;
    Ok(tx_objs
        .iter()
        .map(|tx| tx.get_stacks_transaction())
        .collect::<Result<Vec<_>, _>>()
        .map_err(serde::de::Error::custom)?
        .into_iter()
        .flatten()
        .collect::<Vec<_>>())
}
```

**File:** stackslib/src/net/http/error.rs (L39-54)
```rust
    if content_type == HttpContentType::Text {
        let mut error_text = String::new();
        let mut ioc = io::Cursor::new(body);
        let mut bound_fd =
            BoundReader::from_reader(&mut ioc, body.len().min(MAX_MESSAGE_LEN as usize) as u64);
        bound_fd
            .read_to_string(&mut error_text)
            .map_err(Error::ReadError)?;

        Ok(HttpResponsePayload::Text(error_text))
    } else if content_type == HttpContentType::JSON {
        let mut ioc = io::Cursor::new(body);
        let mut bound_fd =
            BoundReader::from_reader(&mut ioc, body.len().min(MAX_MESSAGE_LEN as usize) as u64);
        let json_val = serde_json::from_reader(&mut bound_fd)
            .map_err(|_| Error::DecodeError("Failed to decode JSON".to_string()))?;
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L45-50)
```text
# REQUIRED: Authorization password for the node's block proposal endpoint.
#
# WARNING: This MUST match the `auth_token` in the stacks-node's
# [connection_options] section. If they do not match, the signer
# cannot communicate with the node and will fail silently.
auth_password = "<YOUR_AUTH_PASSWORD>"
```
