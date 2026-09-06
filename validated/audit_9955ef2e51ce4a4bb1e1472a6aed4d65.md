### Title
Unbounded attacker-supplied `estimated_len` in `/v2/fees/transaction` causes u64 multiplication overflow/panic - ([File: stackslib/src/net/api/postfeerate.rs])

### Summary
The `POST /v2/fees/transaction` handler accepts a client-supplied `estimated_len: Option<u64>` field with no upper-bound validation, taking the max of it and the actual decoded payload length. This attacker-controlled value is later multiplied by `MINIMUM_TX_FEE_RATE_PER_BYTE` without any overflow check, causing a panic in debug builds and silent wraparound in release builds.

### Finding Description
`try_parse_request` validates only that `content_len` (the HTTP body length) is within `(0, MAX_PAYLOAD_LEN)` [1](#0-0) , but this bound applies to the JSON request body size, not to the `estimated_len` field value inside that JSON, which is deserialized directly as `Option<u64>` with no range check [2](#0-1) . The handler computes `estimated_len = max(body.estimated_len.unwrap_or(0), payload_data.len() as u64)` [3](#0-2) , so a small valid hex payload combined with `"estimated_len": 18446744073709551615` in the JSON yields `estimated_len = u64::MAX`, completely decoupled from `MAX_PAYLOAD_LEN`/actual payload size.

This value flows unchanged through `try_handle_request` into `estimate_tx_fee_from_cost_and_length`, where `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` performs an unchecked u64 multiplication [4](#0-3) . The workspace `Cargo.toml` does not set `overflow-checks = true` for the release profile [5](#0-4) , so in release builds this multiplication silently wraps (Rust's default behavior), while in debug builds (default `overflow-checks = true`) it panics, crashing the thread handling the request.

The only preconditions are that the node has fee/cost estimators configured (`rpc_args.get_estimators_ref()` returns `Some`), reachable via `stackslib/src/net/api/postfeerate.rs` lines 219-237 [6](#0-5) , a routine and common node configuration. No authentication, secret, or privileged role is required — this is a public RPC endpoint.

### Impact Explanation
On a debug build (or any build where `overflow-checks` is enabled, which is common in test/staging deployments and matches Rust's own debug default), a single crafted POST panics the thread handling the request, crashing or degrading that RPC worker — a remote unauthenticated DoS from one message. On a standard release build, the multiplication silently wraps, producing a nonsensical `minimum_fee` value that is applied to every fee estimate returned in that response, corrupting the fee data served to the caller (though this does not persist across nodes/requests since `estimated_len` is per-request state, not global). The debug-build DoS matches the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Trivial to trigger: the attacker needs only network access to the node's RPC port (no secret/auth), fee estimators enabled (a common, non-exotic configuration), and a single valid-looking JSON body with a tiny hex payload and `estimated_len: u64::MAX`. Fully repeatable per request.

### Recommendation
Validate `body.estimated_len` against a sane upper bound (e.g., `MAX_PAYLOAD_LEN` or `MAX_TRANSACTION_LEN`) in `try_parse_request` before accepting it, rejecting the request otherwise; additionally use `checked_mul`/`saturating_mul` instead of the raw `*` at `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length` to defend in depth against overflow.

### Proof of Concept
Add a test in `stackslib/src/net/api/postfeerate.rs` (or its test module) that:
1. Constructs a `FeeRateEstimateRequestBody` with a minimal valid hex `transaction_payload` (e.g., a small `TransactionPayload::TokenTransfer` serialization) and `estimated_len: Some(u64::MAX)`.
2. Serializes it to JSON and calls `RPCPostFeeRateRequestHandler::try_parse_request` with a matching `HttpRequestPreamble` (content-length within bounds, content-type JSON).
3. Asserts `handler.estimated_len == Some(u64::MAX)`.
4. Calls `RPCPostFeeRateRequestHandler::estimate_tx_fee_from_cost_and_length(..., estimated_len=u64::MAX, ...)` directly with a dummy `FeeEstimator`/`CostMetric`.
5. Observe: in a debug build this panics at `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` (line 112); the test should instead assert the function returns a bounded `Err(...)` after the fix, or that `estimated_len` was clamped before reaching this line.

### Citations

**File:** stackslib/src/net/api/postfeerate.rs (L37-42)
```rust
#[derive(Serialize, Deserialize)]
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

**File:** stackslib/src/net/api/postfeerate.rs (L219-237)
```rust
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

**File:** Cargo.toml (L75-78)
```text
[profile.release]
debug = true
codegen-units = 1
lto = "fat"
```
