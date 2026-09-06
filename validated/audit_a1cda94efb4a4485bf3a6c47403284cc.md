### Title
Unbounded `pages_indexes` token parsing before `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` check - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`RPCGetAttachmentsInvRequestHandler::try_parse_request` splits the `pages_indexes` query parameter on `,` and calls `.parse::<u32>()` on every resulting token, inserting each valid value into a `HashSet<u32>`, with no bound on the number of tokens processed. The `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) check is only applied afterward, in `try_handle_request`, so an attacker can force the handler to do parsing/hashing work proportional to the size of the query string before rejection.

### Finding Description
In `try_parse_request` [1](#0-0) , the code iterates over every comma-separated substring of the `pages_indexes` value, parses it as `u32`, and inserts it into a `HashSet<u32>` with no limit on how many tokens are processed. The cap `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` is only enforced later in `try_handle_request` [2](#0-1) , after parsing has already completed and after the `HashSet` has already been fully populated and then collected/sorted into a `Vec<u32>` [3](#0-2) .

This confirms the claimed fault: the number of tokens parsed into `page_indexes` is not bounded relative to `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` at parse time — it is bounded only by whatever limits exist on overall HTTP request-line/query-string length elsewhere in the HTTP layer (e.g., header/request-line size caps in `stackslib/src/net/httpcore.rs` and `stackslib/src/net/http/request.rs`). Since those limits are generic transport-level caps (typically several KB to tens of KB), a query string filled with many short numeric tokens (e.g., `1,2,3,...`) can still contain thousands of tokens within that budget, all of which get `.parse()`'d and hashed before the 8-item cap is checked.

### Impact Explanation
This is a bounded compute cost, not unbounded: the amount of work is capped by the maximum permitted HTTP request/query length enforced by the HTTP framing layer, not by application logic. Each request causes the node to do parsing and hashing work proportional to the query string length rather than to the intended cap of 8 page indexes, on the unauthenticated read endpoint `/v2/attachments/inv`. This matches the "bounded compute DoS on a read endpoint" category (High) rather than an unbounded/unauthenticated crash — it does not corrupt state, forge data, or crash the process; it only wastes CPU/allocation proportional to input size before rejecting the request.

### Likelihood Explanation
The endpoint requires no authentication and no privileged role — any remote client that can reach the node's RPC port can send this request. The attacker's cost is trivial (constructing one query string within the transport-level size limit), and the request is repeatable at will, but the actual amount of wasted compute per request is small and bounded by the HTTP layer's own size limits, which were not investigated in exhaustive detail here (the specific numeric caps in `stackslib/src/net/http/request.rs` / `stackslib/src/net/httpcore.rs` were not fully confirmed within the available tool budget).

### Recommendation
Enforce the `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` limit inside `try_parse_request`, e.g., by counting tokens (or checking `page_indexes.len()`) as they are inserted and returning `Error::DecodeError` as soon as the count exceeds the cap, instead of deferring the check to `try_handle_request`.

### Proof of Concept
Construct an HTTP GET request to `/v2/attachments/inv?index_block_hash=<valid hex>&pages_indexes=<N comma-separated u32 values>` where N is large enough to fill the maximum permitted query-string/request-line length, and call `RPCGetAttachmentsInvRequestHandler::try_parse_request` directly with this preamble/query. Assert that CPU time / allocations scale with N up to the transport-level max, and that the `page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` rejection in `try_handle_request` only occurs after all tokens have already been parsed and hashed.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L94-101)
```rust
            } else if key == "pages_indexes" {
                let pages_indexes_value = value.to_string();
                for entry in pages_indexes_value.split(',') {
                    if let Ok(page_index) = entry.parse::<u32>() {
                        page_indexes.insert(page_index);
                    }
                }
            }
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L118-122)
```rust
        let mut page_index_list: Vec<u32> = page_indexes.into_iter().collect();
        page_index_list.sort();

        self.index_block_hash = Some(index_block_hash);
        self.page_indexes = Some(page_index_list);
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L159-168)
```rust
        if page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST {
            let msg = format!(
                "Number of attachment inv pages is limited by {} per request",
                MAX_ATTACHMENT_INV_PAGES_PER_REQUEST
            );
            warn!("{msg}");
            return StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new(msg))
                .try_into_contents()
                .map_err(NetError::from);
        }
```
