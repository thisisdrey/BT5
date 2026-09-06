### Title
Unchecked `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` multiplication overflow in `estimate_tx_fee_from_cost_and_length` - ([File: stackslib/src/net/api/postfeerate.rs])

### Summary
The `/v2/fees/transaction` handler takes `body.estimated_len` directly from the client-supplied JSON, floors it with `payload_data.len()` via `max()`, and then multiplies it by `MINIMUM_TX_FEE_RATE_PER_BYTE` at line 112 using plain `u64` multiplication with no `checked_mul`/`saturating_mul`. Since `body.estimated_len: Option<u64>` is attacker-controlled and unconstrained by any range/cap check, a value like `u64::MAX` (or any value where `estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE` exceeds `u64::MAX`) causes the multiplication to overflow.

### Finding Description
`try_parse_request` (lines 179-181) computes `estimated_len = max(body.estimated_len.unwrap_or(0), payload_data.len())`. Because `max` only floors the value, an attacker can freely set `body.estimated_len` to `u64::MAX`, which passes through unchanged as `estimated_len`. This value is stored on `self.estimated_len` and later passed to `estimate_tx_fee_from_cost_and_length` (lines 205-235), where line 112 does:

```rust
let minimum_fee = estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE;
``` [1](#0-0) 

This is unchecked arithmetic on a fully attacker-controlled `u64`. In a debug build (`cargo build`, as used in typical CI/test builds), this triggers a Rust runtime panic ("attempt to multiply with overflow"), which — since this code executes inside the RPC request-handling path via `node.with_node_state` — would abort/crash the thread handling the request. In a release build, standard Rust semantics wrap the multiplication silently (release profiles do not enable overflow checks by default in this repo, as no `overflow-checks = true` setting was found in `Cargo.toml`), so `minimum_fee` silently wraps to an arbitrary, likely small value, breaking the intended safety property that `fee >= estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE`. This is reachable by any remote unauthenticated caller — the `/v2/fees/transaction` POST endpoint has no `is_authenticated`/RPC-secret gate distinguishable from other public read endpoints in this file (no such check appears in `try_parse_request` or `try_handle_request`), and the only sanitization is a body-length bound (`content_len < MAX_PAYLOAD_LEN`) and JSON/hex decoding of the transaction payload, none of which constrain `estimated_len`'s magnitude.

### Impact Explanation
- In debug builds: a single crafted POST panics the request-handling thread — a remote, unauthenticated, single-request DoS.
- In release builds: the response's `RPCFeeEstimateResponse.estimations[].fee` values silently violate the documented minimum-fee floor guarantee, returning an incorrect (wrapped, arbitrarily small) fee for the reported `estimated_len`. This could steer a wallet/client consuming this public fee-estimation API to construct a transaction with an insufficient fee for its stated size, though this is a data-integrity/informational issue on a fee-estimation advisory API rather than mempool acceptance itself (mempool admission has its own independent fee-floor checks elsewhere in `chainstate/stacks/db/blocks.rs`, not affected by this endpoint).

The debug-build panic path most closely matches "single-request integer-overflow panic" impact.

### Likelihood Explanation
Trivial: the endpoint is a standard public RPC path (`/v2/fees/transaction`), requires no secret, no peer relationship, and no special role — a single crafted POST with `estimated_len: 18446744073709551615` (`u64::MAX`) and any minimal valid hex-encoded `TransactionPayload` triggers the code path. Attacker cost is one HTTP request.

### Recommendation
Replace the raw multiplication at postfeerate.rs line 112 with checked/saturating arithmetic, e.g.:
```rust
let minimum_fee = estimated_len.saturating_mul(MINIMUM_TX_FEE_RATE_PER_BYTE);
```
and/or clamp `body.estimated_len` to a sane maximum (e.g., `MAX_PAYLOAD_LEN` or a small multiple thereof) during `try_parse_request`, rejecting values that are clearly not plausible transaction lengths.

### Proof of Concept
Add a test in `stackslib/src/net/api/tests/postfeerate.rs` that:
1. Constructs a `FeeRateEstimateRequestBody` with `estimated_len: Some(u64::MAX)` and a minimal valid hex `transaction_payload` (e.g., a `TokenTransfer` payload).
2. Sends it via the existing test harness used for this handler (mirroring the existing `postfeerate` tests that call `try_parse_request`/`try_handle_request`).
3. In a debug-profile test run, assert that invoking `RPCPostFeeRateRequestHandler::estimate_tx_fee_from_cost_and_length` (or the full `try_handle_request`) either panics with `"attempt to multiply with overflow"` (caught via `std::panic::catch_unwind`) or, once fixed, returns an `Err`/saturated result rather than a wrapped/incorrect `minimum_fee`.
4. Explicit assertion: `assert!(result_fee >= estimated_len.saturating_mul(MINIMUM_TX_FEE_RATE_PER_BYTE) || panic_caught)`, confirming the equality `fee == max(scalar_fee, estimated_len * MINIMUM_TX_FEE_RATE_PER_BYTE)` no longer silently wraps.

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
