### Title
Remote arithmetic-overflow panic via unbounded `estimated_len` in `/v2/fees/transaction` - (File: stackslib/src/net/api/postfeerate.rs)

### Summary
The `estimated_len` field in the JSON body of `POST /v2/fees/transaction` is fully attacker-controlled and is bounded only from below (via `max()` with the actual decoded payload length), never from above. This value flows unchecked into an unchecked `u64` multiplication `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length`, which can overflow.

### Finding Description
In `try_parse_request` [1](#0-0) , `estimated_len` is computed as `std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64)`. `body.estimated_len` is deserialized directly from attacker JSON as an `Option<u64>` with no upper bound check [2](#0-1) . The only length check performed is on the HTTP body's `content_length` against `MAX_PAYLOAD_LEN` [3](#0-2) , which constrains the size of the raw HTTP request body, not the numeric value of the JSON-encoded `estimated_len` field. An attacker can send a small request body containing `{"estimated_len": 18446744073709551615, "transaction_payload": "0x..."}` with a tiny valid payload, satisfying the body-length check while setting `estimated_len` to `u64::MAX`.

This value is stored in `self.estimated_len` and later passed unchanged into `estimate_tx_fee_from_cost_and_length` via `try_handle_request` [4](#0-3) , where the multiplication occurs: `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` [5](#0-4) . This is a plain `*` operator with no `checked_mul`/`saturating_mul`, so in a debug or overflow-checked build this panics; in a standard release build it silently wraps.

This is reachable only when the node has cost/fee estimators configured (`rpc_args.get_estimators_ref()` returns `Some`) [6](#0-5) , which is a common/default RPC node configuration, not a privileged precondition — it does not require the RPC secret or any special role.

### Impact Explanation
A single unauthenticated POST to `/v2/fees/transaction` can crash the node process if built with overflow checks enabled (e.g., debug builds, or any release profile compiled with `overflow-checks = true`), since integer overflow panics unwind/abort the thread handling the RPC request. Whether this crashes the whole node or only aborts the request thread depends on how the HTTP request handling thread panics are caught elsewhere in the codebase; regardless, the underlying fault — an unchecked multiplication on a value with no upper bound derived directly from remote input — is a real bug matching a remote-DoS category on the RPC endpoint, repeatable on every request. In standard release builds without overflow-checks, Rust's default wrapping behavior avoids a panic but this is incidental / build-profile dependent, not an intentional guard in the code.

### Likelihood Explanation
Precondition: node must have cost/fee estimators enabled, which is a standard node configuration option and not a privileged secret. The attacker needs no RPC secret, no P2P handshake, and only needs to reach the node's RPC port with a single crafted small HTTP POST body. This is trivially repeatable and costs the attacker essentially nothing (one HTTP request).

### Recommendation
Bound `estimated_len` from `try_parse_request` to a sane maximum (e.g., `MAX_PAYLOAD_LEN` or the maximum transaction size), rejecting values above it with a `400`. Additionally, replace the raw multiplication in `estimate_tx_fee_from_cost_and_length` with `estimated_len.checked_mul(MINIMUM_TX_FEE_RATE_PER_BYTE)` (or `saturating_mul`) and return an HTTP error on overflow rather than allowing a panic/wrap.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postfeerate.rs` that constructs an `HttpRequestPreamble` for `POST /v2/fees/transaction` with a JSON body `{"estimated_len": 18446744073709551615, "transaction_payload": "0x0000...(valid minimal TransactionPayload hex)"}`, invoke `RPCPostFeeRateRequestHandler::try_parse_request` followed by `try_handle_request` against a `StacksNodeState` configured with `get_estimators_ref()` returning `Some`, and run the test suite with `RUSTFLAGS="-C overflow-checks=on"` (or `cargo test` in debug profile, which enables overflow checks by default). Assert that the call either returns an `Err`/HTTP 400 response or, if it panics, that this constitutes the observed crash at `stackslib/src/net/api/postfeerate.rs:112` (`estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE`).

### Citations

**File:** stackslib/src/net/api/postfeerate.rs (L38-42)
```rust
pub struct FeeRateEstimateRequestBody {
    #[serde(default)]
    pub estimated_len: Option<u64>,
    pub transaction_payload: String,
}
```

**File:** stackslib/src/net/api/postfeerate.rs (L112-112)
```rust
        let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
```

**File:** stackslib/src/net/api/postfeerate.rs (L152-158)
```rust
        let content_len = preamble.get_content_length();
        if !(content_len > 0 && content_len < MAX_PAYLOAD_LEN) {
            return Err(Error::DecodeError(format!(
                "Invalid Http request: invalid body length for FeeRateEstimate ({})",
                content_len
            )));
        }
```

**File:** stackslib/src/net/api/postfeerate.rs (L180-181)
```rust
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);
```

**File:** stackslib/src/net/api/postfeerate.rs (L205-237)
```rust
        let estimated_len = self
            .estimated_len
            .take()
            .ok_or(NetError::SendError("`estimated_len` not set".into()))?;
        let tx = self
            .transaction_payload
            .take()
            .ok_or(NetError::SendError("`transaction_payload` not set".into()))?;

        let data_resp =
            node.with_node_state(|_network, sortdb, _chainstate, _mempool, rpc_args| {
                let tip = self.get_canonical_burn_chain_tip(&preamble, sortdb)?;
                let stacks_epoch = self.get_stacks_epoch(&preamble, sortdb, tip.block_height)?;

                if let Some((cost_estimator, fee_estimator, metric)) = rpc_args.get_estimators_ref()
                {
                    let estimated_cost = cost_estimator
                        .estimate_cost(&tx, &stacks_epoch.epoch_id)
                        .map_err(|e| {
                            StacksHttpResponse::new_error(
                                &preamble,
                                &HttpBadRequest::new_json(e.into_json()),
                            )
                        })?;

                    Self::estimate_tx_fee_from_cost_and_length(
                        &preamble,
                        fee_estimator,
                        metric,
                        estimated_cost,
                        estimated_len,
                        stacks_epoch,
                    )
```
