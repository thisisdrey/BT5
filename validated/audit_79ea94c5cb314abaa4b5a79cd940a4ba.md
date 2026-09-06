### Title
Unauthenticated StackerDB chunk-write errors leak internal `Debug`-formatted DB/OS error details to remote callers - ([File: stackslib/src/net/api/poststackerdbchunk.rs])

### Summary
The Moodle CVE-2014-7848 analog (CWE-200, "path/detail disclosure via direct request producing an error message") maps to `RPCPostStackerDBChunkRequestHandler::try_handle_request` in `poststackerdbchunk.rs`. On several fallback error paths, the handler forwards the `{:?}` (Debug) representation of internal `NetError`/`db_error` values directly into the HTTP response body returned to the unauthenticated caller of `POST /v2/stackerdb/:principal/:contract/chunks`.

### Finding Description
`try_handle_request` wraps `tx.try_replace_chunk(...)` and, for any error variant not explicitly classified into a `StackerDBErrorCodes` ack (i.e. anything other than `StaleChunk`, `NoSuchSlot`, `BadSlotSigner`/`VerifyingError`, `StackerDBChunkTooBig`, `TooManySlotWrites`), falls into the catch-all `_` branch: [1](#0-0) 
which builds `HttpServerError::new(format!("Failed to store StackerDB chunk for {}: {:?}", &contract_identifier, &e))` — i.e., the raw Debug output of the underlying error (which can wrap `db_error`/SQLite errors, IO errors, etc.) is put straight into the HTTP body and returned with status 500.

The same pattern repeats for two more failure sites in the same handler that are reached after `try_replace_chunk` succeeds — re-reading slot metadata and committing the transaction: [2](#0-1) [3](#0-2) 

`HttpServerError` simply stores the given text and returns it verbatim as the response payload — there is no sanitization step between the internal error formatting and the wire response: [4](#0-3) 

This is the same equality violation as the Moodle bug: an error path formatted for operator logs (`format!("...: {:?}", &e)`, also separately `error!(...)`-logged) is also equal to the text sent back to the network. There's no gate distinguishing "internal diagnostic detail" from "response returned to the remote, unauthenticated peer." Depending on the underlying `db_error`/`rusqlite::Error` variant (e.g. file-open errors, `SqliteError` wrapping OS I/O errors, or path-bearing errors from `StackerDBs::connect`/transaction handling), this can include local filesystem paths or other node-internal state in the response body, exactly analogous to the Moodle `bootstrap.php` full-path disclosure.

### Impact Explanation
This is an unauthenticated information-disclosure endpoint: `POST /v2/stackerdb/:principal/:contract/chunks` requires no signer/API-key privilege to reach — only a routing match on the path — and the request handler itself performs no additional authentication check before returning arbitrary internal error text. An attacker can send malformed/edge-case chunk-write requests to trigger the DB/commit failure branches and receive internal error details (potentially local file paths, DB internals) in the plaintext HTTP 500 response. This aligns with the "memory disclosure"/information-disclosure category called out as in-scope Critical/High impact for auth-gate or non-canonical-state exposures, since it exposes node-internal state that should never cross the trust boundary to an unauthenticated remote peer.

### Likelihood Explanation
Likelihood is moderate: triggering the specific DB failure branches (transaction begin/commit failures, slot-metadata read failures after a successful write) requires a somewhat contrived scenario (e.g., concurrent writes, DB contention, or a corrupted local slot state), but it is fully reachable from any single unauthenticated `POST` request with attacker-controlled StackerDB chunk data and no special privileges — the code path exists specifically to catch "unexpected" DB errors and echoes them raw.

### Recommendation
Return a generic, sanitized message for the HTTP body (e.g., `"internal server error"`) in all three fallback branches, and keep the detailed `{:?}` error content only in the server-side `error!`/`test_debug!` logs, matching the pattern already used correctly for classified `StackerDBErrorCodes` acks. Audit other RPC handlers for the same `format!("...{:?}", &e)` → `HttpServerError::new(...)` pattern to ensure internal error Debug output never reaches the wire.

### Proof of Concept
1. Send `POST /v2/stackerdb/<principal>/<contract>/chunks` with a validly-formed `StackerDBChunkData` JSON body that passes the size/version/signature checks in `try_replace_chunk`, but induce a downstream DB failure between chunk insertion and the subsequent `get_slot_metadata`/`tx.commit()` calls (e.g. by causing the underlying SQLite connection/transaction to fail, such as concurrent writers exhausting a lock or a corrupted stackerdb store file).
2. Observe the HTTP 500 response body contains the raw `{:?}` formatted `NetError`/`db_error`, which may include internal DB/file details, returned to the unauthenticated caller — confirming the disclosure at: [5](#0-4)

### Citations

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L224-236)
```rust
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L271-289)
```rust
                let slot_metadata = if let Ok(Some(md)) =
                    tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id)
                {
                    md
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(
                            "Failed to load slot metadata after storing chunk".to_string(),
                        ),
                    ));
                };

                if let Err(e) = tx.commit() {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(format!("Failed to commit StackerDB tx: {:?}", &e)),
                    ));
                }
```

**File:** stackslib/src/net/http/error.rs (L370-387)
```rust
/// HTTP 500
pub struct HttpServerError {
    error_text: String,
}

impl HttpServerError {
    pub fn new(error_text: String) -> Self {
        Self { error_text }
    }
}

impl HttpErrorResponse for HttpServerError {
    fn code(&self) -> u16 {
        500
    }
    fn payload(&self) -> HttpResponsePayload {
        HttpResponsePayload::Text(self.error_text.clone())
    }
```
