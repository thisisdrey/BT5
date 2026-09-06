### Title
Unbounded page-index parsing before length check in `RPCGetAttachmentsInvRequestHandler::try_parse_request` - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`try_parse_request` parses every comma-separated value in the `pages_indexes` query parameter into a `HashSet<u32>` with no cap on the number of entries. The `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) bound is only enforced later in `try_handle_request`, after the full set has already been built, allowing an unauthenticated caller to force CPU/memory work proportional to the size of a single query string.

### Finding Description
In `try_parse_request` (stackslib/src/net/api/getattachmentsinv.rs), the `pages_indexes` query value is split on `,` and each token that parses as `u32` is inserted into a `HashSet<u32>` with no bound: [1](#0-0) 

Only after parsing completes, sorted, and the request is dispatched to `try_handle_request` is the size of `page_indexes` checked against `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`: [2](#0-1) 

The endpoint is registered without authentication (`security: []` per the question's context and typical convention for this RPC route), so any remote TCP client reaching `/v2/attachments/inv` can send a query string with a very large number of unique comma-separated `u32` values (e.g. `pages_indexes=0,1,2,...,N`). The handler allocates a `HashSet<u32>`, performs a hash-insert for every token, then sorts a `Vec<u32>` of that size — all attacker-controlled compute/memory — before the 400 rejection fires. This confirms the equality claimed in the prompt: the number of entries hashed before the bound is enforced is unconstrained by `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`, rather than being capped at 8 during parsing.

Whether the maximum achievable N is limited by an upstream HTTP request-line/header length cap in the generic HTTP preamble parser was not confirmed in this investigation (no such constant was found in `stackslib/src/net/http.rs` or `stackslib/src/net/httpcore.rs` during the search). Even if such a generic cap exists, it is not specific to this endpoint's semantics and does not change the fact that this handler performs unbounded parsing/hashing work relative to its own declared per-request limit (8) before checking that limit.

### Impact Explanation
An unauthenticated remote attacker can force a target node to spend CPU and memory building a `HashSet<u32>` and sorting a `Vec<u32>` sized to the number of unique tokens in a single query string, only to have the request rejected afterward with HTTP 400. This is a bounded, single-message compute/memory amplification on a read RPC endpoint — matching the "bounded compute DoS on a read endpoint" High-severity category. It is trivially repeatable (one HTTP GET per attempt) and requires no privileges, secrets, or peer relationship with the node.

### Likelihood Explanation
No preconditions are required beyond TCP reachability to the node's RPC port; the endpoint has no authentication. The attacker cost is a single crafted HTTP GET request with a large query string. The attack is fully repeatable and can be sent concurrently from multiple connections to multiply the effect, though each individual request's cost is what's being flagged here (bounded per-message compute inflation, not raw connection/bandwidth flooding).

### Recommendation
Enforce the `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` bound during parsing rather than after: track the count of unique tokens inserted into `page_indexes` in `try_parse_request` and short-circuit with `Error::DecodeError` as soon as the count exceeds the limit (e.g., 8), avoiding processing of the remainder of the query string.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/getattachmentsinv.rs`:
1. Build an `HttpRequestPreamble` for `GET /v2/attachments/inv?index_block_hash=<64-hex>&pages_indexes=0,1,2,...,999999` (a query string with ~1,000,000 unique comma-separated `u32` values).
2. Call `RPCGetAttachmentsInvRequestHandler::try_parse_request` directly with this preamble and query string.
3. Assert that `self.page_indexes.as_ref().unwrap().len()` is on the order of 1,000,000 (i.e., far exceeds `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` = 8) and/or measure wall-clock/allocation cost of the call to demonstrate the amplification, showing that the length check that would reject this request only happens afterward in `try_handle_request`, not during `try_parse_request`.

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
