### Title
`/v2/mempool/query` (`RPCMempoolQueryRequestHandler`) performs no authorization check, allowing unauthenticated mempool transaction disclosure - ([File: stackslib/src/net/api/postmempoolquery.rs])

### Summary
`RPCMempoolQueryRequestHandler::try_parse_request` and `try_handle_request` in `postmempoolquery.rs` never reference an `auth`/secret field or the `authorization` header at all, unlike sibling privileged handlers in the same crate. Any remote TCP client that can reach the RPC port can POST a well-formed `MemPoolSyncData` body and receive a streamed page of mempool transactions with HTTP 200, with zero secret required.

### Finding Description
The struct `RPCMempoolQueryRequestHandler` is defined with only `page_id` and `mempool_query` fields — no `auth: Option<String>` field exists [1](#0-0) . Its `try_parse_request` only validates `preamble.get_content_length() != 0` and then calls `MemPoolSyncData::consensus_deserialize(&mut body_ptr)` — there is no `preamble.headers.get("authorization")` lookup anywhere in the file [2](#0-1) . This is in stark contrast to comparable/adjacent privileged handlers in the same module family, e.g. `RPCBlockProposalRequestHandler::try_parse_request` [3](#0-2) , `RPCNakamotoBlockSimulateRequestHandler::try_parse_request` [4](#0-3) , `RPCNakamotoBlockReplayRequestHandler::try_parse_request` [5](#0-4) , `RPCFastCallReadOnlyRequestHandler::try_parse_request` [6](#0-5) , and `RPCTransactionSimulateRequestHandler::try_parse_request` [7](#0-6)  — all of which store an `auth: Option<String>` and reject with 400 (if no secret configured) or 401 (missing/mismatched header) before touching request-specific data.

`try_handle_request` then unconditionally opens the mempool via `mempool.reopen(false)` and constructs a `StacksMemPoolStream`, streaming transaction bytes back with a 200 OK regardless of any header content [8](#0-7) . The streaming logic itself, `StacksMemPoolStream::generate_next_chunk`, walks `MemPoolDB::static_find_next_missing_transactions` and serializes matching transactions with no additional authorization gate [9](#0-8) .

The claimed invariant — "requests receiving a streamed mempool tx page must carry the configured secret" — does not hold: the set of requests that receive a stream equals the set of all syntactically valid requests (nonzero body, well-formed `MemPoolSyncData`), independent of the `authorization` header's presence or value.

### Impact Explanation
Any remote, unprivileged TCP client can read the full contents of a node's mempool transaction set (transaction bodies, sender/nonce/fee metadata) by sending repeated POSTs to `/v2/mempool/query` with a valid `MemPoolSyncData` and paging via the returned `page_id`. This is an unauthenticated read/disclosure of `MemPoolDB` state, matching the "memory disclosure"/"unauthenticated read" Critical category. It is fully repeatable and requires no privileged role, secret, or prior handshake — it is a plain unauthenticated RPC call.

### Likelihood Explanation
No preconditions beyond TCP reachability to the node's RPC port (default `/v2/*` API, always enabled on a stacks-node). The attacker needs no peer registration, no StackerDB slot, no secret, and no local access — a single crafted HTTP POST with a valid `MemPoolSyncData` payload suffices, and it can be repeated indefinitely to page through the entire mempool.

### Recommendation
Add an `auth: Option<String>` field to `RPCMempoolQueryRequestHandler` (populated from the node's configured `auth_token`, consistent with `RPCBlockProposalRequestHandler`/`RPCNakamotoBlockSimulateRequestHandler`), and in `try_parse_request` require: if no secret configured, return 400 (endpoint disabled); if secret configured, require `preamble.headers.get("authorization")` to be present and equal to the secret, else return 401 — mirroring the pattern in `postblock_proposal.rs`/`blocksimulate.rs`/`blockreplay.rs`/`fastcallreadonly.rs`/`txsimulate.rs`.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postmempoolquery.rs` analogous to `postblock_proposal::test_try_parse_request`:
1. Build a `StacksHttpRequest` for `/v2/mempool/query` via `StacksHttpRequest::new_mempool_query(host, MemPoolSyncData::TxTags(...), None)`.
2. Do NOT add an `authorization` header (or add an arbitrary wrong one, e.g. `request.add_header("authorization".into(), "garbage".into())`).
3. Serialize and run through `http.handle_try_parse_request(&mut RPCMempoolQueryRequestHandler::new(), &parsed_preamble.expect_request(), &bytes[offset..])`.
4. Assert the call succeeds (`Ok(..)`) rather than an `Err(NetError::Http(Error::Http(401, ..)))`, then proceed to `try_handle_request` on a `StacksNodeState` with a populated mempool and assert the response preamble status is `200` and the streamed body contains serialized `StacksTransaction`/`Txid` bytes — demonstrating no authorization gate exists at `stackslib/src/net/api/postmempoolquery.rs:234-256` and `:266-316`.

### Citations

**File:** stackslib/src/net/api/postmempoolquery.rs (L35-47)
```rust
#[derive(Clone)]
pub struct RPCMempoolQueryRequestHandler {
    pub page_id: Option<Txid>,
    pub mempool_query: Option<MemPoolSyncData>,
}

impl RPCMempoolQueryRequestHandler {
    pub fn new() -> Self {
        Self {
            page_id: None,
            mempool_query: None,
        }
    }
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L126-156)
```rust
    fn generate_next_chunk(&mut self) -> Result<Vec<u8>, String> {
        if self.corked {
            test_debug!(
                "Finished streaming txs; last page was {:?}",
                &self.last_randomized_txid
            );
            return Ok(vec![]);
        }

        if self.num_txs >= self.max_txs || self.finished {
            test_debug!(
                "Finished sending transactions after {:?}. Corking tx stream.",
                &self.last_randomized_txid
            );

            // cork the stream -- send the next page_id the requester should use to continue
            // streaming.
            self.corked = true;
            return Ok(self.last_randomized_txid.serialize_to_vec());
        }

        let remaining = self.max_txs.saturating_sub(self.num_txs);
        let (next_txs, next_last_randomized_txid_opt, num_rows_visited) =
            MemPoolDB::static_find_next_missing_transactions(
                &self.mempool_db,
                &self.tx_query,
                self.coinbase_height,
                &self.last_randomized_txid,
                1,
                remaining,
            )
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L234-256)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected nonzero body length".to_string(),
            ));
        }

        let mut body_ptr = body;
        let mempool_body = MemPoolSyncData::consensus_deserialize(&mut body_ptr)?;

        self.mempool_query = Some(mempool_body);
        if let Some(page_id) = self.get_page_id_query(query) {
            self.page_id = Some(page_id);
        }
        Ok(HttpRequestContents::new().query_string(query))
    }
}
```

**File:** stackslib/src/net/api/postmempoolquery.rs (L278-316)
```rust
        let stream_res = node.with_node_state(|network, _sortdb, _chainstate, mempool, _rpc_args| {
            let coinbase_height = network.stacks_tip.coinbase_height;
            let max_txs = network.connection_opts.mempool_max_tx_query;
            debug!(
                "Begin mempool query";
                "page_id" => %page_id.as_ref().map(|txid| format!("{txid}")).unwrap_or("(none".to_string()),
                "coinbase_height" => coinbase_height,
                "max_txs" => max_txs
            );

            let mempool_db = match mempool.reopen(false) {
                Ok(db) => db,
                Err(e) => {
                    return Err(StacksHttpResponse::new_error(&preamble, &HttpServerError::new(format!("Failed to open mempool DB: {:?}", &e))));
                }
            };

            Ok(StacksMemPoolStream::new(mempool_db, mempool_query, max_txs, coinbase_height, page_id))
        });

        let stream = match stream_res {
            Ok(stream) => stream,
            Err(response) => {
                return response.try_into_contents().map_err(NetError::from);
            }
        };

        let resp_preamble = HttpResponsePreamble::from_http_request_preamble(
            &preamble,
            200,
            "OK",
            None,
            HttpContentType::Bytes,
        );
        Ok((
            resp_preamble,
            HttpResponseContents::from_stream(Box::new(stream)),
        ))
    }
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1128-1144)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        _captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block proposal endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blocksimulate.rs (L145-161)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block replay endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/blockreplay.rs (L567-583)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        _body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block replay endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/fastcallreadonly.rs (L100-110)
```rust
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block proposal endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** stackslib/src/net/api/txsimulate.rs (L350-360)
```rust
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the transaction simulation endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```
