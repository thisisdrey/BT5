### Title
Unbounded `estimated_len` in POST /v2/fees/transaction causes integer overflow/panic in fee computation - (File: stackslib/src/net/api/postfeerate.rs)

### Summary
`try_parse_request` accepts an attacker-supplied `estimated_len` field in the JSON body with no upper-bound validation, only taking `max()` with the actual decoded payload length. This value flows unchecked into `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length`, which can overflow/panic on a plain `u64` multiplication.

### Finding Description
The claimed equality is: "estimated_len used for minimum-fee computation == a length actually bounded by MAX_PAYLOAD_LEN/MAX_TRANSACTION_LEN." This equality does not hold. `content_len` (the HTTP body length) is bounded by `MAX_PAYLOAD_LEN` [1](#0-0) , but `body.estimated_len` is a free-standing `Option<u64>` field deserialized directly from JSON with no relationship to `content_len` or to the actual hex-decoded payload size [2](#0-1) . The code computes `estimated_len = max(body.estimated_len.unwrap_or(0), payload_data.len())`, so an attacker who sets `estimated_len: 18446744073709551615` in the JSON causes this max to resolve to `u64::MAX` regardless of the real payload size [3](#0-2) . This value is stored and later passed unchanged to `estimate_tx_fee_from_cost_and_length`, where line `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` performs a plain (non-checked) `u64` multiplication [4](#0-3) . In a debug build this multiplication panics on overflow (`attempt to multiply with overflow`), crashing the RPC handler thread on the very first crafted request; in a release build it silently wraps to an incorrect value that gets served as the minimum fee to every subsequent estimator response for that request.

None of the existing guards catch this: `content_len` bound only checks the HTTP body size, not the JSON field value; `hex_bytes`/`consensus_deserialize` only validate the transaction payload bytes, not `estimated_len`; there is no `will_admit_mempool_tx` check on this read-only estimation endpoint; and there is no cap comparing `estimated_len` to `MAX_PAYLOAD_LEN` or `MAX_TRANSACTION_LEN` anywhere in this path.

### Impact Explanation
A single unauthenticated POST to `/v2/fees/transaction` (with fee/cost estimators configured, i.e. `rpc_args.get_estimators_ref()` returns `Some`) can panic the handling thread in debug builds — an unauthenticated remote crash/DoS from one message, matching the Critical category. In release builds the wrapped multiplication silently corrupts the `minimum_fee` value returned to the caller (and potentially misleads fee estimation for that response), though this does not persist state or affect other requests since `estimated_len` is per-request, not cached globally.

### Likelihood Explanation
No privileged access is required — this is a standard RPC POST endpoint reachable by any remote party who can connect to the node's RPC port. The only precondition is that the node has fee/cost estimators configured, which is a common/default node configuration. The attacker cost is a single crafted HTTP request with a minimal valid hex transaction payload and `"estimated_len": 18446744073709551615` in the JSON body. The attack is trivially repeatable against any node running with debug assertions enabled (debug builds panic every time); release builds silently produce wrong (but non-crashing) values.

### Recommendation
Bound `body.estimated_len` (and the resulting `estimated_len`) to a sane maximum (e.g., `MAX_TRANSACTION_LEN` / `MAX_PAYLOAD_LEN`) in `try_parse_request`, rejecting the request with `Error::DecodeError` if it exceeds this bound. Additionally, use `checked_mul` or `saturating_mul` instead of the raw `*` operator in `estimate_tx_fee_from_cost_and_length` (line 112) to prevent panics/wraparound regardless of upstream validation.

### Proof of Concept
Add a test in `stackslib/src/net/api/postfeerate.rs` (or its test module) that:
1. Constructs a minimal valid `TransactionPayload` hex string (small size, e.g. a `TokenTransfer` or `Coinbase` payload) and serializes a `FeeRateEstimateRequestBody { estimated_len: Some(u64::MAX), transaction_payload: <hex> }` JSON body.
2. Builds an `HttpRequestPreamble` with `content_length` set to the JSON body's actual byte length (which is `< MAX_PAYLOAD_LEN`), and calls `RPCPostFeeRateRequestHandler::try_parse_request` on it — confirm it succeeds and sets `self.estimated_len = Some(u64::MAX)`.
3. Calls `RPCPostFeeRateRequestHandler::estimate_tx_fee_from_cost_and_length` directly with `estimated_len = u64::MAX` and a fixed `MINIMUM_TX_FEE_RATE_PER_BYTE` constant, asserting that in a debug build this panics with `attempt to multiply with overflow` at line 112, or in release wraps to an incorrect `minimum_fee` — demonstrating the missing bound-check/overflow-safe arithmetic rather than a graceful `Err` response.

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

**File:** stackslib/src/net/api/postfeerate.rs (L179-184)
```rust
        let tx = TransactionPayload::consensus_deserialize(&mut payload_data.as_slice())?;
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);

        self.transaction_payload = Some(tx);
        self.estimated_len = Some(estimated_len);
```
