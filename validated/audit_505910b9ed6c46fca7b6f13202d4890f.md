### Title
Unauthenticated `POST /v2/stackerdb/:principal/:contract_name/chunks` endpoint returns raw internal error debug output (DB/internal error details) in HTTP 500 body - (File: stackslib/src/net/api/poststackerdbchunk.rs)

### Summary
The `RPCPostStackerDBChunkRequestHandler::try_handle_request` fallback branch for unclassified `NetError`s embeds the `Debug`-formatted underlying error directly into the HTTP response body sent back to the (unauthenticated, remote) requester, regardless of node debug settings, mirroring the Umbraco CVE-2024-43376 pattern of leaking internal error/stack-trace detail through API error responses.

### Finding Description
When a StackerDB chunk write via `try_replace_chunk` fails with an error that isn't one of the explicitly classified `NetError` variants (`StaleChunk`, `NoSuchSlot`, `BadSlotSigner`/`VerifyingError`, `StackerDBChunkTooBig`, `TooManySlotWrites`), the handler falls into the catch-all branch and returns: [1](#0-0) 
which builds the HTTP 500 body as `format!("Failed to store StackerDB chunk for {}: {:?}", &contract_identifier, &e)` — i.e., the `Debug` representation of the internal `NetError` (which can wrap a database error, `rusqlite`/`db_error`, or other internal fault) is serialized verbatim into the text body returned to the caller [2](#0-1) .

The same anti-pattern (unclassified error `Debug`-formatted into the 500 body) recurs a few lines later for the "load slot metadata after storing" and "commit tx" failure paths: [3](#0-2) [4](#0-3) 

Unlike the Umbraco case (leaked .NET stack traces), this Rust code does not include a call stack, but it does propagate internal error *contents* — for example `db_error` (rusqlite/SQLite failure text, which can include SQL fragments or file-system-adjacent detail) — directly to a network peer via `NetError::from(db_error)` chains that ultimately reach `try_replace_chunk`. This breaks the intended equality that "client-facing error text" should be a curated, safe message versus "internal error detail," which should stay server-side only (analogous to Umbraco leaking internal detail regardless of debug-mode being off).

I could not fully enumerate every possible caller of `try_replace_chunk` that could produce a DB-layer error (e.g., corrupted DB state, disk I/O failure) within the available index; `stackslib/src/net/mod.rs` shows a `DBError` variant on `net::Error` used elsewhere in the crate but I was not able to inspect its exact `Display`/`Debug` output within the remaining iteration budget.

### Impact Explanation
This is a remote, unauthenticated information-disclosure issue reachable by any peer that can reach the `/v2/stackerdb/.../chunks` RPC endpoint (a normal peer-facing HTTP endpoint, not requiring any privileged role). The disclosed content is internal error detail (potentially including DB-layer diagnostic strings), which is lower severity than a full stack trace but matches the same CWE-209 class as the source advisory: an endpoint returning implementation detail in error responses irrespective of any "debug" toggle. It does not by itself grant write access, forge data, or crash the node, so it sits below the Critical/High bar defined in this scan's Impact rubric (no unauthorized write, no forged-data propagation, no crash, no auth bypass) — it is best characterized as a lower-severity information-disclosure issue rather than a High/Critical finding under the stated impact categories.

### Likelihood Explanation
Triggering the fallback branch requires an internal/DB-layer error during a legitimate-looking POST — e.g. transient SQLite failures, disk contention, or any future `NetError` variant not yet classified in the match arms. This is plausible under normal operational conditions (not solely adversary-controlled), making it low-effort to observe but not deterministically triggerable by an external attacker without a way to force a DB fault.

### Recommendation
Replace the catch-all branch's client-facing message with a generic, static string (e.g., `"Internal error while storing StackerDB chunk"`), and keep the detailed `{:?}` error only in the server-side `error!` log call that already exists at lines 225-228. Apply the same fix to the "load slot metadata" (240-256) and "commit tx" (284-289) branches, which have the identical pattern.

### Proof of Concept
1. Trigger any internal error in `try_replace_chunk` other than the five explicitly classified `NetError` variants (e.g., a simulated DB I/O failure).
2. Observe the HTTP 500 response body from `POST /v2/stackerdb/<principal>/<contract>/chunks` contains `format!("Failed to store StackerDB chunk for {}: {:?}", &contract_identifier, &e)`, exposing the `Debug` output of the internal error to the remote caller. [5](#0-4)

### Citations

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L163-236)
```rust
    fn try_handle_request(
        &mut self,
        preamble: HttpRequestPreamble,
        _contents: HttpRequestContents,
        node: &mut StacksNodeState,
    ) -> Result<(HttpResponsePreamble, HttpResponseContents), NetError> {
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
        let http_peer = node.http_peer_addr();

        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
                    test_debug!(
                        "Failed to replace chunk {}.{} in {}: {:?}",
                        stackerdb_chunk.slot_id,
                        stackerdb_chunk.slot_version,
                        &contract_identifier,
                        &e
                    );
                    // Classify the rejection directly from the error. `StaleChunk` is the
                    // only retryable case (the normal version-bump handshake); everything
                    // else is terminal for an identical chunk. Anything unexpected (DB or
                    // internal error) is a server error, not a client-classifiable ack, so
                    // it becomes an HTTP 500 rather than a misleading `accepted: false`.
                    let err_code = match &e {
                        NetError::StaleChunk { .. } => StackerDBErrorCodes::DataAlreadyExists,
                        NetError::NoSuchSlot(..) => StackerDBErrorCodes::NoSuchSlot,
                        NetError::BadSlotSigner(..) | NetError::VerifyingError(..) => {
                            StackerDBErrorCodes::BadSigner
                        }
                        NetError::StackerDBChunkTooBig(..) => StackerDBErrorCodes::ChunkTooBig,
                        NetError::TooManySlotWrites { .. } => {
                            StackerDBErrorCodes::TooManySlotWrites
                        }
                        _ => {
                            error!("Failed to replace StackerDB chunk with an unexpected error";
                                   "smart_contract_id" => contract_identifier.to_string(),
                                   "error" => format!("{:?}", &e)
                            );
                            return Err(StacksHttpResponse::new_error(
                                &preamble,
                                &HttpServerError::new(format!(
                                    "Failed to store StackerDB chunk for {}: {:?}",
                                    &contract_identifier, &e
                                )),
                            ));
                        }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L240-256)
```rust
                    let slot_metadata_opt =
                        match tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id) {
                            Ok(slot_opt) => slot_opt,
                            Err(e) => {
                                // some other error
                                error!("Failed to load replaced StackerDB chunk metadata";
                                       "smart_contract_id" => contract_identifier.to_string(),
                                       "error" => format!("{:?}", &e)
                                );
                                return Err(StacksHttpResponse::new_error(
                                    &preamble,
                                    &HttpServerError::new(format!(
                                        "Failed to load StackerDB chunk for {}: {:?}",
                                        &contract_identifier, &e
                                    )),
                                ));
                            }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L284-289)
```rust
                if let Err(e) = tx.commit() {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(format!("Failed to commit StackerDB tx: {:?}", &e)),
                    ));
                }
```
