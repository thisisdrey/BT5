### Title
Unchecked multiplication of attacker-controlled `estimated_len` causes overflow panic/wrap in `estimate_tx_fee_from_cost_and_length` - ([File: stackslib/src/net/api/postfeerate.rs])

### Summary
`RPCPostFeeRateRequestHandler::try_parse_request` sets `estimated_len` to `max(body.estimated_len, payload_data.len())`, where `body.estimated_len` is an arbitrary attacker-supplied `u64` field in the JSON body, completely decoupled from the actual HTTP body size limit (`MAX_PAYLOAD_LEN`) which only bounds the raw HTTP content length, not the numeric value of a JSON integer field. This attacker-controlled value is later multiplied by `MINIMUM_TX_FEE_RATE_PER_BYTE` with a plain `*` operator with no overflow check.

### Finding Description
In `try_parse_request`, the only size restriction applied is on the *HTTP body's byte length* via `preamble.get_content_length()` checked against `MAX_PAYLOAD_LEN` [1](#0-0) . This has no bearing on the numeric value of the `estimated_len` JSON field itself — a tiny JSON body such as `{"estimated_len":18446744073709551615,"transaction_payload":"0x00"}` easily fits under any reasonable payload cap while carrying `u64::MAX` as the declared value. The handler computes: [2](#0-1) 
with no upper bound check on `body.estimated_len`.

This value is stored in `self.estimated_len` and later passed into `estimate_tx_fee_from_cost_and_length`, where: [3](#0-2) 
performs `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` using the plain `*` operator, not `checked_mul`, `saturating_mul`, or `wrapping_mul`. With `estimated_len = u64::MAX` and `MINIMUM_TX_FEE_RATE_PER_BYTE` being a small positive constant (≥1), the multiplication overflows a `u64`. In a build with overflow checks enabled (Rust debug builds, or any release build compiled with `overflow-checks = true`), this triggers an immediate panic within the request-handling thread. In a standard release build (overflow checks off, default panic=unwind semantics for arithmetic), the multiplication silently wraps, producing a garbage `minimum_fee` value that is then compared against and potentially substitutes each `RPCFeeEstimate.fee` in the JSON response returned to the caller as an authoritative fee floor.

Neither `try_parse_request` nor `estimate_tx_fee_from_cost_and_length` clamps `estimated_len` to any sane ceiling (e.g., `MAX_PAYLOAD_LEN` or a block-size-derived limit) before this arithmetic, so the only bound in play is the HTTP content-length cap, which constrains the *request size* but not the *declared numeric field value*.

### Impact Explanation
Any unauthenticated remote caller with basic HTTP access to a node's RPC port can send a single small POST request to `/v2/fees/transaction` and either crash the request-handling path (in overflow-checked builds) or receive a bogus/wrapped `minimum_fee` in the response of a node compiled without overflow checks, which could mislead client wallets querying fee estimates. This is a single-request, repeatable DoS/data-integrity issue confined to the fee-estimation RPC endpoint and does not affect consensus state, mempool admission, or block validation.

### Likelihood Explanation
No special peer relationship, StackerDB slot, or secret is required — this endpoint is reachable by any remote client able to issue an HTTP POST to the node's RPC interface, using a minimal, valid-looking JSON body and a trivial `transaction_payload` hex string. The attack costs a single HTTP request and is fully repeatable.

### Recommendation
Bound `estimated_len` to a sane maximum (e.g., clamp to `MAX_PAYLOAD_LEN` or a max transaction/block size constant) in `try_parse_request` before storing it, and/or replace the unchecked multiplication in `estimate_tx_fee_from_cost_and_length` with `checked_mul`/`saturating_mul`, returning a `400 Bad Request` on overflow instead of allowing panic or wraparound.

### Proof of Concept
Add a `stackslib::net::api::postfeerate` test that constructs an `HttpRequestPreamble` with `content-type: application/json` and a small body `{"estimated_len":18446744073709551615,"transaction_payload":"0x00"}`, invoke `RPCPostFeeRateRequestHandler::try_parse_request`, and then call `estimate_tx_fee_from_cost_and_length` with the resulting `estimated_len`. Run under `cargo test` (debug build, overflow checks enabled) and assert the call panics at `stackslib/src/net/api/postfeerate.rs:112`; alternatively, in a release/no-overflow-checks build, assert `minimum_fee` returned in `RPCFeeEstimateResponse` does not equal `estimated_len as u128 * MINIMUM_TX_FEE_RATE_PER_BYTE as u128` truncated to `u64`, demonstrating silent wraparound.

### Citations

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
