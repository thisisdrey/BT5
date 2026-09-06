### Title
Unchecked multiplication of attacker-controlled `estimated_len` by `MINIMUM_TX_FEE_RATE_PER_BYTE` causes overflow panic/DoS on `/v2/fees/transaction` - (File: stackslib/src/net/api/postfeerate.rs)

### Summary
The `/v2/fees/transaction` RPC endpoint accepts a caller-supplied `estimated_len: Option<u64>` field with no upper-bound validation, and uses it directly in an unchecked `u64` multiplication `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length`. A remote unauthenticated caller can set `estimated_len` to a very large value (e.g. `u64::MAX`) alongside a small valid `TransactionPayload`, causing this multiplication to overflow.

### Finding Description
In `try_parse_request` [1](#0-0) , `estimated_len` is computed as `max(body.estimated_len.unwrap_or(0), payload_data.len() as u64)`. `body.estimated_len` comes straight from the deserialized JSON body [2](#0-1)  and is bounded only by `content_len < MAX_PAYLOAD_LEN` for the *overall HTTP body size* [3](#0-2) , not by any check on the numeric value of `estimated_len` itself — a JSON number field can encode `u64::MAX` in a handful of bytes regardless of the payload's actual byte length.

This `estimated_len` is passed unmodified into `estimate_tx_fee_from_cost_and_length`, where the fee floor is computed as:
```rust
let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
``` [4](#0-3) 

This is a plain `*` operator on `u64` values with no `checked_mul`/`saturating_mul`. With `MINIMUM_TX_FEE_RATE_PER_BYTE` being a nonzero constant, setting `estimated_len` near `u64::MAX` (or any value where the product exceeds `u64::MAX`) triggers arithmetic overflow.

None of the existing guards prevent this: `MAX_PAYLOAD_LEN` only bounds the raw HTTP body byte length, not the numeric value of a JSON integer field; there is no authentication gate on this endpoint (it is a public read/estimate endpoint); and there is no `will_admit_mempool_tx`-style validation applied to `estimated_len`, since this handler never touches the mempool — it only calls `cost_estimator.estimate_cost` and `fee_estimator.get_rate_estimates()` before reaching the vulnerable multiplication.

In debug builds, Rust's overflow checks turn this into an immediate panic, crashing (or aborting the request-handling thread of) the node process — remote, unauthenticated, single-message DoS. In release builds (where overflow checks are disabled by default), the multiplication silently wraps, producing a bogus, attacker-steerable `minimum_fee` value that is then applied as a floor to fee-rate estimates returned to any client of the fee-estimation API, corrupting a supposedly canonical/objective estimate service output.

### Impact Explanation
- Debug builds: a single crafted POST to `/v2/fees/transaction` panics the handler thread on overflow, a remote unauthenticated crash — matches "Critical: remote crash/unauthenticated DoS from few messages."
- Release builds: the wraparound produces an arbitrary, attacker-chosen `minimum_fee` (e.g., near zero or any specific value), corrupting the returned fee-rate estimate served by the node to any RPC client — a bounded compute/read-endpoint integrity issue (serving a bogus value as if it were a legitimate floor), matching "High: bounded compute DoS/serving incorrect state on a read endpoint."
- This is trivially repeatable — one HTTP POST per hit, no state or session required.

### Likelihood Explanation
No authentication or RPC secret is required — `/v2/fees/transaction` is a standard public RPC endpoint. The attacker only needs network reachability to the node's RPC port, a syntactically valid small `TransactionPayload` hex, and an `estimated_len` field set to a large integer in the JSON body. Cost is a single HTTP request; no mempool admission, no chain state, and no privileged role is needed.

### Recommendation
Use `checked_mul`/`saturating_mul` for `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` in `estimate_tx_fee_from_cost_and_length`, returning a `400 Bad Request` on overflow (or saturating to `u64::MAX`) instead of panicking or wrapping. Additionally, validate `body.estimated_len` in `try_parse_request` against a sane upper bound (e.g., `MAX_PAYLOAD_LEN` or the node's block byte limit) and reject requests exceeding it with a `DecodeError`.

### Proof of Concept
Add a stackslib net test in `stackslib/src/net/api/tests/postfeerate.rs`:
1. Construct a `FeeRateEstimateRequestBody { estimated_len: Some(u64::MAX), transaction_payload: <hex of a small valid TransactionPayload, e.g., a TokenTransfer> }`.
2. Serialize to JSON and feed as the HTTP body to `RPCPostFeeRateRequestHandler::try_parse_request` with a matching `HttpRequestPreamble` (`Content-Type: application/json`, correct `Content-Length`).
3. Confirm `try_parse_request` succeeds and sets `self.estimated_len = Some(u64::MAX)`.
4. Call `try_handle_request` (or directly `estimate_tx_fee_from_cost_and_length` with `estimated_len = u64::MAX` and a nonzero `MINIMUM_TX_FEE_RATE_PER_BYTE`), and assert that in a debug build (`cargo test`, overflow-checks enabled) the call panics at `stackslib/src/net/api/postfeerate.rs:112`, or in release mode that `minimum_fee` wraps to an incorrect (near-zero) value instead of returning an HTTP 400 error.

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

**File:** stackslib/src/net/api/postfeerate.rs (L179-184)
```rust
        let tx = TransactionPayload::consensus_deserialize(&mut payload_data.as_slice())?;
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);

        self.transaction_payload = Some(tx);
        self.estimated_len = Some(estimated_len);
```
