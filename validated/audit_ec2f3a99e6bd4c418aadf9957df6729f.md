### Title
Remote unauthenticated integer overflow panic/DoS via unbounded `estimated_len` in `/v2/fees/transaction` - (File: stackslib/src/net/api/postfeerate.rs)

### Summary
`RPCPostFeeRateRequestHandler::try_parse_request` takes the attacker-controlled JSON field `estimated_len` and only bounds it via `std::cmp::max` against the actual payload length, never against an upper limit; the only size check performed is on the HTTP body's `content_len` (`< MAX_PAYLOAD_LEN`), which limits the number of JSON bytes, not the numeric magnitude of a `u64` field like `18446744073709551615`. That unbounded value flows unchecked into a plain `u64` multiplication in `estimate_tx_fee_from_cost_and_length` (`estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE`), causing an overflow.

### Finding Description
`try_parse_request` (stackslib/src/net/api/postfeerate.rs:145-186) parses the JSON body into `FeeRateEstimateRequestBody { estimated_len: Option<u64>, transaction_payload: String }` and computes:
```rust
let estimated_len = std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);
``` [1](#0-0) 
There is no upper-bound validation on `body.estimated_len` itself — the pre-check only validates `content_len` (the raw HTTP body byte length) against `MAX_PAYLOAD_LEN`, which does not constrain the numeric value a small JSON payload can encode (a string like `"estimated_len":18446744073709551615` is only ~22 bytes). [2](#0-1) 

This attacker-controlled `estimated_len` is stored on `self.estimated_len` and later passed unmodified into `estimate_tx_fee_from_cost_and_length` during `try_handle_request`:
```rust
let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
``` [3](#0-2) 
This is a plain (non-checked/non-saturating) `u64` multiplication. If `estimated_len` is close to `u64::MAX` and `MINIMUM_TX_FEE_RATE_PER_BYTE` is any value > 1, the multiplication overflows. In debug/overflow-checked builds this panics; in release builds it silently wraps, producing an incorrect `minimum_fee` (a broken fee floor). Neither the content-length guard nor any other validation in the reachable path (`try_parse_request` → `try_handle_request`) constrains the magnitude of the numeric `estimated_len` field before it reaches the multiplication.

### Impact Explanation
Any remote, unauthenticated caller who can reach the node's RPC port can POST to `/v2/fees/transaction` with a crafted JSON body containing a maximal `estimated_len` and a syntactically valid `transaction_payload` hex string, triggering the overflow on every request. This is a single-message, remotely reachable fault:
- In debug/overflow-checked builds, it causes a thread panic, resulting in denial of service for the request-handling thread/task.
- In release builds, arithmetic wraps silently, corrupting the returned `minimum_fee` value in the JSON response served to the client (an incorrect/misleading fee-estimate result, not a state-corruption issue since this is a read-only estimation endpoint with no persisted or gossiped effects).

This matches the "bounded compute DoS on a read endpoint" / crash-from-a-single-message category for the debug-panic case, and is a minor correctness bug (not a security-critical state-corruption) in the release-wrap case, since no consensus, mempool admission, or stored/relayed data is affected — this endpoint only returns a computed fee estimate to the caller.

### Likelihood Explanation
No privileged role, secret, or special peer state is required — this is a standard unauthenticated RPC read endpoint reachable by anyone who can connect to the node's RPC port. The attacker only needs to send one crafted, small HTTP POST request with a valid hex-encoded `TransactionPayload` (e.g. a minimal token-transfer payload) and `estimated_len: u64::MAX`. The attack is trivially repeatable per request.

### Recommendation
Bound `estimated_len` to a sane maximum (e.g., `MAX_PAYLOAD_LEN` or the actual maximum transaction size) in `try_parse_request`, rejecting values above that limit with `Error::DecodeError`. Additionally, replace the raw multiplication in `estimate_tx_fee_from_cost_and_length` with `checked_mul`/`saturating_mul` and return a `StacksHttpResponse` bad-request error (or saturate to `u64::MAX`) on overflow, so the computation is safe regardless of upstream validation.

### Proof of Concept
Add a `stackslib` net test in `stackslib/src/net/api/tests/postfeerate.rs` style:
```rust
#[test]
fn test_overflow_estimated_len() {
    let preamble = /* construct minimal HttpRequestPreamble */;
    let estimated_len = u64::MAX;
    // any positive MINIMUM_TX_FEE_RATE_PER_BYTE will overflow u64::MAX
    let result = std::panic::catch_unwind(|| {
        estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE
    });
    assert!(result.is_err(), "expected overflow panic with overflow-checks enabled");
}
```
Run with `RUSTFLAGS="-C overflow-checks=on" cargo test test_overflow_estimated_len` to observe the panic at `stackslib/src/net/api/postfeerate.rs:112`. For an end-to-end HTTP-level PoC, construct a `StacksHttpRequest::new_post_fee_rate` with `FeeRateEstimateRequestBody { estimated_len: Some(u64::MAX), transaction_payload: "<valid hex TransactionPayload>".into() }`, feed it through `RPCPostFeeRateRequestHandler::try_parse_request` and then `try_handle_request`, and observe the panic (debug build) or the wrapped/incorrect `minimum_fee` value in the JSON response (release build).

### Citations

**File:** stackslib/src/net/api/postfeerate.rs (L110-118)
```rust
        let mut estimations = RPCFeeEstimate::estimate_fees(scalar_cost, fee_rates).to_vec();

        let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;

        for estimate in estimations.iter_mut() {
            if estimate.fee < minimum_fee {
                estimate.fee = minimum_fee;
            }
        }
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

**File:** stackslib/src/net/api/postfeerate.rs (L180-184)
```rust
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);

        self.transaction_payload = Some(tx);
        self.estimated_len = Some(estimated_len);
```
