### Title
Unbounded `pages_indexes` parsing before cap check enables bounded compute DoS - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`RPCGetAttachmentsInvRequestHandler::try_parse_request` splits the attacker-controlled `pages_indexes` query value on `,` and calls `entry.parse::<u32>()` plus `HashSet::insert` for every comma-separated token with no bound on the number of tokens processed. The `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) cap is only checked afterward, and even that check lives in `try_handle_request`, not in the parser itself, so the cap does nothing to stop the parse loop.

### Finding Description
In `try_parse_request` [1](#0-0) , the handler does:
```
} else if key == "pages_indexes" {
    let pages_indexes_value = value.to_string();
    for entry in pages_indexes_value.split(',') {
        if let Ok(page_index) = entry.parse::<u32>() {
            page_indexes.insert(page_index);
        }
    }
}
```
There is no limit on the number of `,`-separated tokens iterated, parsed, or inserted into the `HashSet<u32>` before or during this loop. The only size check, `page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`, happens later in `try_handle_request` [2](#0-1) , and it checks the *deduplicated* set size, not the number of tokens processed. An attacker can send a query string with an arbitrarily large number of comma-separated integers (duplicates or a long ascending/descending run that all collapse into ≤8 unique `u32` values, e.g. all `1`s), forcing the server to perform `parse::<u32>()` and `HashSet::insert` proportional to the attacker-chosen input length while still passing the final `len() > 8` check.

The `HttpRequestPreamble`/`content_length` check only guards the request body (`preamble.get_content_length() != 0`) [3](#0-2) , not the URL/query string length, so nothing in this handler bounds query length. I could not locate, within the available index, an explicit global URL/header-length cap enforced ahead of dispatch to this handler in `net/http` or `net/httpcore`; if such a cap exists elsewhere in the HTTP preamble reader it would bound the practical severity, but I found no such bound in the reachable code for this handler.

### Impact Explanation
Each malicious request forces the node to perform O(N) integer parses and hash-set insertions where N is the attacker-chosen number of comma-separated tokens in the query string, rather than being bounded by the documented 8-page cap. This is a bounded per-request compute/allocation cost inflation on an unauthenticated (`security: []`) read endpoint (`GET /v2/attachments/inv`), matching the "bounded compute DoS on a read endpoint" High-impact category. It does not corrupt state, forge data, or crash the node outright; it is a CPU-cost amplification per request, repeatable by any remote client.

### Likelihood Explanation
No preconditions beyond network reachability to the node's RPC port; the endpoint is unauthenticated per the OpenAPI spec. The attacker only needs to craft one HTTP GET request with a large `pages_indexes` value (limited only by whatever underlying HTTP layer/header size limits exist, which were not confirmed to be bounded specifically against this query string in the reachable code). Cost to the attacker is a single request; the amplification factor depends on how large a query string the server accepts.

### Recommendation
Bound the number of tokens processed in the `pages_indexes` split loop to `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (or a small multiple) and break out of the loop early once that bound is exceeded, returning a `DecodeError`/`HttpBadRequest` immediately instead of continuing to parse and insert. Additionally, consider capping the total length of the `pages_indexes` query value that is accepted before splitting.

### Proof of Concept
Rust test in `stackslib/src/net/api/getattachmentsinv.rs` (or a new test module):
1. Build a `query_str` like `"index_block_hash=<64 hex>&pages_indexes="` followed by e.g. 1,000,000 comma-separated `"1"` tokens.
2. Call `RPCGetAttachmentsInvRequestHandler::try_parse_request` with this query and measure/assert that the number of `parse::<u32>()`/`insert` operations performed (e.g., via instrumenting the loop or timing) scales linearly with the token count even though `page_indexes.len()` ends up `== 1` (well under `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`), demonstrating the parser does unbounded work before any cap is applied.

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L73-77)
```rust
        if preamble.get_content_length() != 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected 0-length body".to_string(),
            ));
        }
```

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
