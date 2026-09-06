### No vulnerability found for this question.

The target function `RPCGetStackerDBChunkRequestHandler::new()` in `stackslib/src/net/api/getstackerdbchunk.rs` is a trivial constructor that only initializes three `Option` fields to `None`; it performs no tx handling, storage, or admission logic whatsoever. [1](#0-0) 

The entire file implements a read-only HTTP `GET` endpoint (`/v2/stackerdb/:principal/:contract_name/:slot_id/:slot_version`) that fetches an already-stored StackerDB chunk and returns it to the caller; it never writes to the mempool, never calls or bypasses `will_admit_mempool_tx`, and has no relay/store path for transactions at all. [2](#0-1) 

There is no code path in this file connecting StackerDB chunk retrieval to mempool tx storage, so the claimed invariant break ("every stored tx == one that passed admission") is not applicable to this target. The question's premise does not match the actual behavior of the cited file/function.

### Citations

**File:** stackslib/src/net/api/getstackerdbchunk.rs (L37-45)
```rust
impl RPCGetStackerDBChunkRequestHandler {
    pub fn new() -> Self {
        Self {
            contract_identifier: None,
            slot_id: None,
            slot_version: None,
        }
    }
}
```

**File:** stackslib/src/net/api/getstackerdbchunk.rs (L110-171)
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
        let slot_id = self
            .slot_id
            .take()
            .ok_or(NetError::SendError("`slot_id` not set".into()))?;
        let slot_version = self.slot_version.take();

        let chunk_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let chunk_res = if let Some(version) = slot_version.as_ref() {
                    network
                        .get_stackerdbs()
                        .get_chunk(&contract_identifier, slot_id, *version)
                        .map(|chunk_data| chunk_data.map(|chunk_data| chunk_data.data))
                } else {
                    network
                        .get_stackerdbs()
                        .get_latest_chunk(&contract_identifier, slot_id)
                };

                match chunk_res {
                    Ok(Some(chunk)) => {
                        debug!(
                            "Loaded {}-byte chunk for {} slot {} version {:?}",
                            chunk.len(),
                            &contract_identifier,
                            slot_id,
                            &slot_version
                        );
                        Ok(chunk)
                    }
                    Ok(None) | Err(NetError::NoSuchStackerDB(..)) => {
                        // not found
                        Err(StacksHttpResponse::new_error(
                            &preamble,
                            &HttpNotFound::new("StackerDB contract or chunk not found".to_string()),
                        ))
                    }
                    Err(e) => {
                        // some other error
                        error!("Failed to load StackerDB chunk";
                               "smart_contract_id" => contract_identifier.to_string(),
                               "slot_id" => slot_id,
                               "slot_version" => slot_version,
                               "error" => format!("{:?}", &e)
                        );
                        Err(StacksHttpResponse::new_error(
                            &preamble,
                            &HttpServerError::new("Failed to load StackerDB chunk".to_string()),
                        ))
                    }
                }
            });
```
