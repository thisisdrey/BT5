### Title
Unbounded page_indexes parsing before MAX_ATTACHMENT_INV_PAGES_PER_REQUEST check enables parse-time compute DoS - ([File: stackslib/src/net/api/getattachmentsinv.rs])

### Summary
`RPCGetAttachmentsInvRequestHandler::try_parse_request` parses the entire `pages_indexes` query parameter into a `HashSet<u32>` with no upper bound on the number of entries, only after this parsing/allocation completes does `try_handle_request` check the count against `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`. A remote unauthenticated client can send a single GET request with a very large comma-separated list to force disproportionate CPU/memory work per request before rejection.

### Finding Description
`try_parse_request` (lines 66-125) iterates over every comma-separated token in the `pages_indexes` query value, parses each as `u32`, and inserts it into a `HashSet<u32>` [1](#0-0) , with no limit on the number of tokens processed at this stage. Only later, in `try_handle_request`, is `page_indexes.len()` compared against `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) and the request rejected if it exceeds that [2](#0-1) . The intended equality — "number of page indexes actually processed is bounded by MAX_ATTACHMENT_INV_PAGES_PER_REQUEST before any work is done" — does not hold: the parse step does unbounded work (string splitting, integer parsing, hash-set insertion, and a final `sort()` over the whole collected set [3](#0-2) ) proportional to the size of the attacker-supplied query string, and this happens irrespective of the eventual 8-item cap enforced afterward. An attacker only needs to craft a URL such as `/v2/attachments/inv?index_block_hash=<64-hex>&pages_indexes=1,2,3,...,N` with N in the tens or hundreds of thousands; because integers can be packed densely (short numeric tokens separated by single commas), a query string of a few hundred KB to a few MB can encode hundreds of thousands of page indices, all of which get parsed and hashed before the cheap length check finally rejects the request.

### Impact Explanation
Each such request forces the node's RPC-handling thread to perform O(N) string splitting, integer parsing, and hash-set insertion/sort work for an unauthenticated, unprivileged `GET /v2/attachments/inv` request that is ultimately rejected. Because the endpoint requires no authentication and no session or peer trust, this can be repeated cheaply and continuously by any remote client with network access to the RPC port, degrading RPC responsiveness. This matches the "bounded compute DoS on a read endpoint" category — it is bounded per request (limited by total HTTP body/query size limits elsewhere in the stack), but is attacker-controlled and disproportionate to the legitimate 8-page cap.

### Likelihood Explanation
The precondition is only that the attacker can reach the node's public RPC port (`/v2/attachments/inv`), which requires no secret, peer key, or StackerDB slot — it is a plain read endpoint. Attacker cost is a single crafted HTTP GET request; the cost to the node is proportional to the number of comma-separated tokens supplied, which the attacker fully controls up to whatever overall HTTP request/query size limit exists elsewhere in the HTTP request-reading path. This is trivially repeatable at negligible attacker bandwidth cost.

### Recommendation
Enforce the `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` bound during parsing itself in `try_parse_request`: count parsed tokens (or cap on `page_indexes.len()`) as they are inserted into the `HashSet`, and return `Error::DecodeError` immediately once the count exceeds the maximum, before continuing to parse the remainder of the query string. This prevents any request specifying more than the permitted number of pages from incurring more than O(MAX_ATTACHMENT_INV_PAGES_PER_REQUEST) parsing cost.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/getattachmentsinv.rs` (or a new net test) that constructs an `HttpRequestPreamble` and query string `pages_indexes=` followed by 100,000 comma-separated integers plus a valid `index_block_hash`, and calls `RPCGetAttachmentsInvRequestHandler::try_parse_request` directly (bypassing `try_handle_request`). Assert/measure that `try_parse_request` returns `Ok` with `self.page_indexes` containing 100,000 entries (i.e., parsing succeeds and fully materializes the oversized set) prior to any call into `try_handle_request`'s `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` check, demonstrating the parse-time cost is incurred regardless of the eventual rejection.

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

**File:** stackslib/src/net/api/getattachmentsinv.rs (L118-119)
```rust
        let mut page_index_list: Vec<u32> = page_indexes.into_iter().collect();
        page_index_list.sort();
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
