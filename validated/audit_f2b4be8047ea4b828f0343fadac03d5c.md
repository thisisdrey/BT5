### No vulnerability found for this question.

`decode_account_entry_response` in `stackslib/src/net/api/getaccount.rs` is a client-side helper that parses a JSON `AccountEntryResponse` from an HTTP response body when *this* node acts as an HTTP client calling another node's `/v2/accounts/:principal` endpoint [1](#0-0) . It is only invoked from its own test suite (`stackslib/src/net/api/tests/getaccount.rs`) and has no callers anywhere in the relay, mempool admission, StackerDB, Atlas, or inventory code.

Searching for all usages of `decode_account_entry_response`/`new_getaccount` across the repo turns up only the definition and its unit tests — no relay path, mempool-insertion code, or `will_admit_mempool_tx` caller references it [2](#0-1) . Likewise, `will_admit_mempool_tx` is defined/used in `stackslib/src/core/mempool.rs`, `stackslib/src/chainstate/stacks/db/blocks.rs`, and `stackslib/src/net/tests/relay/epoch2x.rs`, none of which reference `decode_account_entry_response` or `getaccount.rs` at all [3](#0-2) .

Since the target function is not on any path that receives attacker-controlled bytes over P2P/RPC that leads to tx storage, and it has no relationship to mempool admission (`will_admit_mempool_tx`), the claimed equality-break ("a relayed tx bypasses `will_admit_mempool_tx`" via this function) cannot occur here — there is no code path connecting the two. The function is only reachable as a local API-response decoder for outbound HTTP requests this node itself issues, not as a handler for inbound attacker bytes.

### Citations

**File:** stackslib/src/net/api/getaccount.rs (L242-269)
```rust
impl StacksHttpRequest {
    /// Make a new request for an account
    pub fn new_getaccount(
        host: PeerHost,
        principal: PrincipalData,
        tip_req: TipRequest,
        with_proof: bool,
    ) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            host,
            "GET".into(),
            format!("/v2/accounts/{}", &principal),
            HttpRequestContents::new()
                .for_tip(tip_req)
                .query_arg("proof".into(), if with_proof { "1" } else { "0" }.into()),
        )
        .expect("FATAL: failed to construct request from infallible data")
    }
}

impl StacksHttpResponse {
    pub fn decode_account_entry_response(self) -> Result<AccountEntryResponse, NetError> {
        let contents = self.get_http_payload_ok()?;
        let contents_json: serde_json::Value = contents.try_into()?;
        let resp: AccountEntryResponse = serde_json::from_value(contents_json)
            .map_err(|_e| NetError::DeserializeError("Failed to load from JSON".to_string()))?;
        Ok(resp)
    }
```

**File:** stackslib/src/core/mempool.rs (L1-1)
```rust
// Copyright (C) 2013-2020 Blockstack PBC, a public benefit corporation
```
