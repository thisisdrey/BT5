### Title
Single bad `pages_indexes` entry causes `HttpNotFound` to discard already-fetched valid attachment pages - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`RPCGetAttachmentsInvRequestHandler::try_handle_request` iterates over up to `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) requested page indexes, accumulating successfully-fetched `AttachmentPage`s into `pages`. If any single page index causes `get_attachments_available_at_page_index` to return `Err`, the handler immediately returns `HttpNotFound`, discarding all previously accumulated valid pages instead of returning the partial, valid result.

### Finding Description
In `try_handle_request` [1](#0-0) , the loop calls `network.get_atlasdb().get_attachments_available_at_page_index(*page_index, &index_block_hash)` for each requested page index and on `Ok` pushes the page into the `pages` vector, but on the first `Err(msg)` it returns early with `HttpNotFound`, discarding `pages` entirely:
```
match page_res {
    Ok(page) => { pages.push(page); }
    Err(msg) => {
        return StacksHttpResponse::new_error(&preamble, &HttpNotFound::new(msg))
            .try_into_contents()
            .map_err(NetError::from);
    }
}
```
This is an all-or-nothing aggregation over a batch request where a single failing element (e.g., an out-of-range page index producing a DB error) unconditionally overrides the fully valid pages already computed for earlier indexes in the same request, matching the "rejection drops valid message" pattern.

An unprivileged remote caller controls `pages_indexes` fully via the query string parser [2](#0-1) , with only an upper bound of 8 distinct page indexes and no bound rejecting large page indices, so a request combining a valid low page index with a crafted large/overflowing page index will unconditionally suppress the previously good pages, since the whole response short-circuits to 404.

I was not able to fully inspect `get_attachments_available_at_page_index` in `stackslib/src/net/atlas/db.rs` within the available tool budget (the file content did not load beyond the license header), so I cannot confirm from source in this session exactly which page index values trigger a `db_error::Overflow` (e.g., via `page_index * ATTACHMENTS_INV_PAGE_SIZE` overflow or an SQL query error) versus simply returning an empty/zero inventory page. This detail is necessary to fully confirm the specific proof-of-concept value near `u32::MAX / ATTACHMENTS_INV_PAGE_SIZE`, though the control-flow fault itself (single error discards all valid pages) is directly confirmed in `try_handle_request`.

### Impact Explanation
The impact is confined to a read-only endpoint (`GET /v2/attachments/inv`) and is a response-integrity/availability defect: a legitimate batched inventory query that includes even one bad page index yields `HttpNotFound` for the entire batch instead of a partial success containing the valid pages. This matches the "bounded compute DoS on a read endpoint" category — the requester (or any downstream code relying on this endpoint, e.g., attachment sync/replication logic) is denied a response for pages it correctly computed, forcing retries or fallback behavior, and repeatable per request at attacker's will since it costs one HTTP GET with a crafted query string.

### Likelihood Explanation
No special preconditions are required: the endpoint is unauthenticated over `/v2/attachments/inv`, reachable by any peer with network access to the RPC port, and the attacker only needs to know one valid page index (or index 0, which is trivially always the first page) alongside a crafted invalid index in the same request. This is a low-cost, single-request, remotely reachable, fully repeatable action.

### Recommendation
Change the per-page loop to collect per-page errors without discarding successfully fetched pages — either (a) skip/omit pages that error and only include successfully-fetched pages in `GetAttachmentsInvResponse`, or (b) include a per-page error/status field in the response schema so the client can distinguish valid pages from failed ones, rather than replacing the entire successful partial result with a single `HttpNotFound`. Additionally, validate/clamp `page_indexes` to a sane range during `try_parse_request` (e.g., reject page indices that would overflow when multiplied by `ATTACHMENTS_INV_PAGE_SIZE`) so malformed indices are rejected with `HttpBadRequest` up front rather than reaching the DB layer at all.

### Proof of Concept
Rust integration test plan (net-level HTTP test) for `getattachmentsinv.rs`:
1. Set up a test `StacksNodeState`/AtlasDB with attachment data present for page index `0` (so `get_attachments_available_at_page_index(0, index_block_hash)` returns `Ok`).
2. Construct a `StacksHttpRequest::new_getattachmentsinv(host, index_block_hash, HashSet::from([0, BAD_INDEX]))` where `BAD_INDEX` is chosen to trigger an `Err` from `get_attachments_available_at_page_index` (e.g., a page index large enough to cause an overflow or DB read error — value to be confirmed against `stackslib/src/net/atlas/db.rs` implementation).
3. Feed the request bytes through `RPCGetAttachmentsInvRequestHandler::try_parse_request` then `try_handle_request`.
4. Assert that the response is `HttpNotFound` (confirming full-batch failure) at the `Err(msg) => return StacksHttpResponse::new_error(&preamble, &HttpNotFound::new(msg))` site in `try_handle_request`, and that no `GetAttachmentsInvResponse` containing the valid page-0 data is ever returned — demonstrating the valid data for page 0 was computed (pushed into `pages`) but discarded.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L94-100)
```rust
            } else if key == "pages_indexes" {
                let pages_indexes_value = value.to_string();
                for entry in pages_indexes_value.split(',') {
                    if let Ok(page_index) = entry.parse::<u32>() {
                        page_indexes.insert(page_index);
                    }
                }
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L177-208)
```rust
        let mut pages = vec![];

        for page_index in page_indexes.iter() {
            let page_res =
                node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                    match network
                        .get_atlasdb()
                        .get_attachments_available_at_page_index(*page_index, &index_block_hash)
                    {
                        Ok(inventory) => Ok(AttachmentPage {
                            inventory,
                            index: *page_index,
                        }),
                        Err(e) => {
                            let msg = format!("Unable to read Atlas DB - {}", e);
                            warn!("{}", msg);
                            Err(msg)
                        }
                    }
                });

            match page_res {
                Ok(page) => {
                    pages.push(page);
                }
                Err(msg) => {
                    return StacksHttpResponse::new_error(&preamble, &HttpNotFound::new(msg))
                        .try_into_contents()
                        .map_err(NetError::from);
                }
            }
        }
```
