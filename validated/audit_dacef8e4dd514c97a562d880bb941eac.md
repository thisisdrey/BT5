### Title
Unbounded `estimated_len` in `POST /v2/fees/transaction` causes integer-overflow panic/silent wraparound in fee computation - (File: stackslib/src/net/api/postfeerate.rs)

### Summary
The `try_parse_request` handler for `POST /v2/fees/transaction` takes the attacker-supplied `estimated_len` field verbatim via `std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64)`, with no upper bound check against `MAX_PAYLOAD_LEN` or any sane limit. This value flows unchecked into `estimate_tx_fee_from_cost_and_length`, where `let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;` performs an unchecked `u64` multiplication.

### Finding Description
In `try_parse_request` [1](#0-0) , the JSON field `estimated_len` (an `Option<u64>`) is only bounded below (via `max` with the actual payload length) and never bounded above. The only size constraint enforced is on the raw HTTP body length: `content_len > 0 && content_len < MAX_PAYLOAD_LEN` [2](#0-1) , which limits the *serialized JSON body size*, not the numeric value of the `estimated_len` field — a client can send a tiny JSON body containing `"estimated_len": 18446744073709551615` alongside a minimal valid hex `transaction_payload`, easily satisfying the content-length check while setting `estimated_len` to `u64::MAX`.

This attacker-controlled value is stored in `self.estimated_len` and later passed into `estimate_tx_fee_from_cost_and_length`, where:
```
let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
``` [3](#0-2) 
is a plain `*` operator with no `checked_mul`/`saturating_mul`. With `estimated_len = u64::MAX` and `MINIMUM_TX_FEE_RATE_PER_BYTE >= 1`, this multiplication overflows `u64`. In debug/overflow-checked builds this panics (thread abort), and in standard release builds (default Rust behavior wraps silently), it produces an incorrect, attacker-steered `minimum_fee` value that no longer reflects the real transaction size — breaking the invariant that `estimated_len` used for fee computation must be consistent with the actual transaction size.

No authentication, secret, or privileged role is required to reach `/v2/fees/transaction`; it is a standard unauthenticated RPC endpoint reachable by any remote peer with network access to the node's RPC port.

### Impact Explanation
- On any build with overflow checks enabled (debug builds, and it's common practice for testnets/CI or hardened builds to enable `overflow-checks=true` in release profiles), a single malicious HTTP POST causes an unhandled arithmetic-overflow panic, crashing the RPC-handling thread/process — an unauthenticated, single-message remote DoS.
- On standard release builds without overflow checks, the multiplication silently wraps, producing an attacker-controlled `minimum_fee` unrelated to the real estimated size, corrupting the fee-estimation response served to any client of this node's RPC API.
- This is trivially repeatable per request and requires no state, funds, or prior interaction.

### Likelihood Explanation
The endpoint is unauthenticated and remotely reachable on any node exposing its RPC HTTP API (default in most configurations). The attacker only needs to craft one small HTTP POST body with a valid hex-decodable `transaction_payload` (even a minimal well-formed `TransactionPayload`) and an `estimated_len` field set to a large value such as `u64::MAX`. No preconditions on peer state, mempool state, or chain tip are required.

### Recommendation
Bound `estimated_len` to a sane maximum (e.g., cap it at `MAX_PAYLOAD_LEN` or the maximum allowed transaction size) in `try_parse_request` before storing it, and/or replace the multiplication in `estimate_tx_fee_from_cost_and_length` with `estimated_len.checked_mul(MINIMUM_TX_FEE_RATE_PER_BYTE)` (or `saturating_mul`), returning an `HttpBadRequest` on overflow/out-of-range input instead of performing raw arithmetic on unchecked, wire-controlled data.

### Proof of Concept
Rust net test plan (in `stackslib/src/net/api/tests/postfeerate.rs` style):
1. Construct an `HttpRequestPreamble` for `POST /v2/fees/transaction` with `Content-Type: application/json`.
2. Build a minimal valid `TransactionPayload` (e.g., a `TokenTransfer` payload), hex-encode it as `transaction_payload`.
3. Set JSON body: `{"estimated_len": 18446744073709551615, "transaction_payload": "0x<hex>"}`.
4. Call `RPCPostFeeRateRequestHandler::try_parse_request` with this body — assert it succeeds and `self.estimated_len == Some(u64::MAX)` (no rejection of the oversized value).
5. Call `estimate_tx_fee_from_cost_and_length` with `estimated_len = u64::MAX` and a `MINIMUM_TX_FEE_RATE_PER_BYTE` constant — assert this either panics with `attempt to multiply with overflow` (in a build compiled with `overflow-checks = true`) or returns a wrapped/incorrect `minimum_fee` value inconsistent with the real payload length (in default release builds), demonstrating the broken invariant.

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

**File:** stackslib/src/net/api/postfeerate.rs (L180-184)
```rust
        let estimated_len =
            std::cmp::max(body.estimated_len.unwrap_or(0), payload_data.len() as u64);

        self.transaction_payload = Some(tx);
        self.estimated_len = Some(estimated_len);
```
