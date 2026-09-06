### Title
Unbounded attacker-controlled `estimated_len` in `/v2/fees/transaction` causes integer-overflow multiplication in `estimate_tx_fee_from_cost_and_length` - ([File: stackslib/src/net/api/postfeerate.rs])

### Summary
`RPCPostFeeRateRequestHandler::try_parse_request` computes `estimated_len = max(body.estimated_len.unwrap_or(0), payload_data.len())`, where `body.estimated_len` is an attacker-supplied `u64` from the JSON POST body with no upper bound. This value is later multiplied by `MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length`, which can panic on overflow in debug builds or silently wrap in release builds.

### Finding Description
The claimed equality is that the `estimated_len` used in fee math should be bounded by the same guard (`content_len < MAX_PAYLOAD_LEN`) that bounds the HTTP body/payload size. That equality does not hold: `content_len` only bounds the size of the raw HTTP JSON body and the hex-decoded `payload_data`, but `body.estimated_len` is a free-standing JSON field deserialized directly as `u64` via `serde_json::from_slice` [1](#0-0) , with no range check applied anywhere. `try_parse_request` then takes `max(body.estimated_len.unwrap_or(0), payload_data.len() as u64)` [2](#0-1) , so an attacker who sets `"estimated_len": 18446744073709551615` (`u64::MAX`) makes `estimated_len` equal to that value regardless of the actual (small, valid) `payload_data` length.

This `estimated_len` flows unmodified into `estimate_tx_fee_from_cost_and_length`, where it is multiplied: `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` [3](#0-2) . `MINIMUM_TX_FEE_RATE_PER_BYTE` is a fixed non-zero constant in `chainstate/stacks/db/blocks.rs`. With `estimated_len = u64::MAX` (or any sufficiently large value), this multiplication overflows `u64`. Since the repo's `Cargo.toml`/workspace does not define a `[profile.release] overflow-checks = true` (none found), the multiplication wraps silently in release builds, producing an essentially garbage `minimum_fee` (could be any value depending on the constant, including near-zero), and in debug builds (where `overflow-checks` defaults to `true`) it panics with "attempt to multiply with overflow", aborting/crashing the thread handling the request.

No authentication or session state is required to reach this: `/v2/fees/transaction` is a public, unauthenticated RPC endpoint reachable by any peer with network access to the RPC port. The only guards present are the `content_len` bound (bounds JSON body size, not the `estimated_len` field's numeric value) and JSON deserialization success — neither prevents an arbitrary large `u64` in the `estimated_len` field.

### Impact Explanation
- In debug builds, a single crafted POST request with a valid minimal `TransactionPayload` and `estimated_len: u64::MAX` causes an unhandled arithmetic-overflow panic inside the request-handling thread, i.e., a remote, unauthenticated, single-message DoS/crash on a publicly reachable endpoint. This matches the Critical category "remote crash/unauthenticated DoS from few messages."
- In release builds, the multiplication wraps, so the returned `minimum_fee` (and by extension the reported `fee` in `RPCFeeEstimateResponse`) is not a truncated/garbage value in the sense of state corruption, but a client relying on this endpoint for fee estimation could receive an incorrect (wrapped, potentially very small) minimum fee value — a data-integrity concern for callers, though this alone is not a crash in release. The primary, clearly demonstrable impact is the debug-build panic path.
- The affected party is any Stacks node running with debug assertions enabled (common in testing/staging deployments, and per stacks-core CI/test builds) exposing the `/v2/fees/transaction` RPC endpoint; the attack is trivially repeatable per request.

### Likelihood Explanation
- No preconditions beyond RPC-port reachability; no secret, peer identity, or StackerDB slot is required.
- The attacker only needs to craft one JSON HTTP POST with a syntactically valid hex `transaction_payload` (any minimal valid `TransactionPayload`, e.g., a `TokenTransfer`) and set `estimated_len` to a large `u64`.
- The endpoint additionally requires `rpc_args.get_estimators_ref()` to return `Some` (i.e., fee/cost estimation configured on the node) to reach the multiplication; this is a common node configuration and not attacker-controlled but also not a privileged precondition — it's a deployment default in most public RPC nodes offering fee endpoints.
- Attacker cost is a single small HTTP request; the issue is fully repeatable.

### Recommendation
Cap `estimated_len` to a sane maximum (e.g., `MAX_PAYLOAD_LEN` or `payload_data.len()` alone, without folding in an unbounded client-supplied value), and/or use checked/saturating arithmetic (`estimated_len.saturating_mul(MINIMUM_TX_FEE_RATE_PER_BYTE)` or `checked_mul` with an error response) in `estimate_tx_fee_from_cost_and_length` before computing `minimum_fee`. Reject requests where `body.estimated_len` exceeds `MAX_PAYLOAD_LEN` (or some reasonable transaction-size ceiling) in `try_parse_request`.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postfeerate.rs` that:
1. Constructs a minimal valid `TransactionPayload` (e.g., a `TokenTransfer`), hex-encodes it as `transaction_payload`.
2. Builds `FeeRateEstimateRequestBody { estimated_len: Some(u64::MAX), transaction_payload: hex }` and serializes to JSON as the POST body of `/v2/fees/transaction`.
3. Feeds the bytes through `RPCPostFeeRateRequestHandler::try_parse_request` (succeeds, since only `content_len` and hex/payload decoding are checked) to obtain `estimated_len = u64::MAX`.
4. Calls `RPCPostFeeRateRequestHandler::estimate_tx_fee_from_cost_and_length` with that `estimated_len` and any `ExecutionCost`/`FeeEstimator`/`CostMetric`.
5. Assert: in a debug build (default `cargo test`), the call panics with "attempt to multiply with overflow" at `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` in `stackslib/src/net/api/postfeerate.rs:112`; wrap the call in `std::panic::catch_unwind` and assert `Err(_)` is returned, confirming the overflow panic path is reachable from attacker-controlled JSON input.

### Citations

**File:** stackslib/src/net/api/postfeerate.rs (L112-112)
```rust
        let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
```

**File:** stackslib/src/net/api/postfeerate.rs (L166-167)
```rust
        let body: FeeRateEstimateRequestBody = serde_json::from_slice(body)
            .map_err(|e| Error::DecodeError(format!("Failed to parse JSON body: {}", e)))?;
```

**File:** stackslib/src/net/api/postfeerate.rs (L179-184)
```rust
        let tx = TransactionPayload::consensus_deserialize(&mut payload_data.as_slice())?;
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);

        self.transaction_payload = Some(tx);
        self.estimated_len = Some(estimated_len);
```
