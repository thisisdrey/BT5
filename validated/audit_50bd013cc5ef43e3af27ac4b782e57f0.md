### Title
Unbounded query-string parsing/hashing before page-count cap check - (File: stackslib/src/net/api/getattachmentsinv.rs)

### Summary
`RPCGetAttachmentsInvRequestHandler::try_parse_request` splits the `pages_indexes` query value on `,` and inserts every successfully-parsed `u32` into a `HashSet` with no bound on the number of tokens processed. The `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST` (8) cap is only enforced later, in `try_handle_request`, after parsing/hashing has already completed, so a single request with many comma-separated tokens forces the server to do unbounded parse+hash work before rejection.

### Finding Description
In `try_parse_request` [1](#0-0) , the handler iterates over `pages_indexes_value.split(',')` and, for every token that parses as `u32`, inserts it into a `HashSet<u32>` — this loop has no size limit and runs entirely before any cap check. The only cap enforcement, `page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`, occurs in `try_handle_request`, which executes strictly after `try_parse_request` has already finished the full split/parse/insert pass [2](#0-1) . Because the HashSet naturally collapses duplicate/overlapping values, an attacker can submit `pages_indexes` containing many thousands of comma-separated tokens (e.g., `0,1,2,...,N` or repeated values) that still end up as ≤8 unique entries after collapsing, forcing O(N) `split`/`parse::<u32>`/hash-insert work for every request before the length check ever runs and the request is rejected.

However, this cost is bounded by the overall HTTP request size limits enforced earlier in the stack (header/request-line/body length limits applied when reading the preamble/query string), not by any check in this file. I was unable to conclusively verify from the available index the exact numeric limit on total query-string/URL length enforced by the HTTP parsing layer (`stackslib/src/net/httpcore.rs`, `stacks-common/src/codec/mod.rs` `MAX_MESSAGE_LEN`), so the precise upper bound of "N" an attacker could reach in one request is unconfirmed from this analysis. What is confirmed via code reading is only that the length check happens after, not before, the parsing loop.

### Impact Explanation
Per request, an attacker forces the node to perform an unbounded (up to whatever the underlying HTTP layer's max request-line/query length allows) number of string-split, integer-parse, and hash-insert operations on a read-only endpoint, before the 8-page cap rejects the request with a 400. This is repeatable per connection/request and does not require any authentication, matching the "bounded compute DoS on a read endpoint" category — but the actual severity depends entirely on the upstream URL/query-length limit which bounds N, and that value was not confirmed in this pass.

### Likelihood Explanation
No privileged access is required; any remote client that can reach the RPC HTTP port can send this request. The attack cost is a single HTTP GET with a long query string. Its practical severity is capped by whatever maximum request-line/header length the HTTP layer enforces (not addressed within `getattachmentsinv.rs`), which I could not fully verify from the indexed code in this session.

### Recommendation
Enforce the token-count cap during parsing itself: while iterating `pages_indexes_value.split(',')`, break/return an error as soon as the number of unique parsed entries (or even raw split tokens) exceeds `MAX_ATTACHMENT_INV_PAGES_PER_REQUEST`, rather than only checking `page_indexes.len()` after the full loop completes in `try_handle_request`. This bounds the parse/hash work to O(MAX_ATTACHMENT_INV_PAGES_PER_REQUEST) regardless of how many tokens a remote peer supplies.

### Proof of Concept
```rust
// stackslib/src/net/api/tests/getattachmentsinv.rs (new test)
#[test]
fn test_try_parse_request_many_tokens_before_cap_check() {
    let addr = SocketAddr::new(IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)), 33333);
    let mut http = StacksHttp::new(addr, &ConnectionOptions::default());

    // Build a pages_indexes value with 10_000 comma-separated tokens
    // that collapse to <= 8 unique values (e.g. cycling 0..8).
    let tokens: Vec<String> = (0..10_000u32).map(|i| format!("{}", i % 8)).collect();
    let query = format!(
        "index_block_hash=1111111111111111111111111111111111111111111111111111111111111111&pages_indexes={}",
        tokens.join(",")
    );
    let request = StacksHttpRequest::new_for_peer(
        addr.into(),
        "GET".into(),
        "/v2/attachments/inv".into(),
        HttpRequestContents::new().query_string(Some(&query)),
    ).unwrap();
    let bytes = request.try_serialize().unwrap();

    let (parsed_preamble, offset) = http.read_preamble(&bytes).unwrap();
    let mut handler = getattachmentsinv::RPCGetAttachmentsInvRequestHandler::new();

    let start = std::time::Instant::now();
    let _ = http.handle_try_parse_request(
        &mut handler,
        &parsed_preamble.expect_request(),
        &bytes[offset..],
    ).unwrap();
    let elapsed = start.elapsed();

    // Assert page_indexes collapsed to <= 8 unique values, confirming
    // the cap-check-after-parse behavior, and log elapsed time to
    // compare against a request with only 8 raw tokens.
    assert!(handler.page_indexes.as_ref().unwrap().len() <= 8);
    eprintln!("parse time for 10,000 tokens: {:?}", elapsed);
}
```
Compare `elapsed` against the same test with only 8 tokens to quantify the per-request parsing overhead attributable to the unbounded pre-cap loop. Note: the maximum achievable token count per single HTTP request is gated by an upstream HTTP request/query-length limit not verified in this analysis.

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
