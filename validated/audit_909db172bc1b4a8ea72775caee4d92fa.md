Confirmed: `fee_ceil(amount)` uses `checked_mul_div_ceil`, so for `amount=1` and any nonzero fee, `fee_ceil(1) = ceil(1 * pips / MAX) ≥ 1`, i.e., 100% tax on a single indivisible unit. This is exactly why `token_fee` special-cases `TokenIdType::Nep245 | TokenIdType::Imt` with `amount <= 1` to `Pips::ZERO` — to avoid confiscating 100% of atomic single-unit MT/IMT transfers. That legitimate exception is exploitable by splitting.

### Title
Protocol fee bypass on NEP-245/IMT `TokenDiff` swaps via unit-splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` deltas whenever `amount <= 1`, a rule meant to stop 100% taxation of atomic single-unit transfers. Because the fee decision is made per-intent on the raw magnitude of that single intent's delta rather than on the signer's aggregate net token movement, an attacker can split one large NEP-245/IMT `TokenDiff` (e.g. delta `-1_000_000`) into `N` separate `TokenDiff` intents each with delta `-1`, batched in a single `execute_intents` call, and pay zero protocol fee on the entire aggregate outflow instead of `Pips::fee_ceil(1_000_000)`.

### Finding Description
The broken binding: fees credited to `fee_collector` for an aggregate NEP-245/IMT outflow of `M` units via `N` unit-delta `TokenDiff`s should equal `protocol_fee.fee_ceil(M)` (same as a single `TokenDiff` with delta `-M`) — but it does not.

Code path: `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:59-78`) iterates `(token_id, delta)` pairs and, for each negative delta, computes fee as: [1](#0-0) 
calling `Self::token_fee(token_id, amount, protocol_fee)`, which for `Nep245`/`Imt` returns `Pips::ZERO` whenever `amount <= 1`: [2](#0-1) 
This check operates purely on the magnitude of the single intent's `delta`, with no memory of how many other `TokenDiff` intents in the same batch (or across batches) touch the same token/signer. `Pips::fee_ceil` uses `checked_mul_div_ceil`, so `fee_ceil(1)` for any nonzero fee rounds up to `1` (100% tax) — this is the actual, legitimate reason for the `amount <= 1` carve-out for NFT-like/MT-atomic transfers.

Exploit: the attacker holds `1_000_000` units of an NEP-245 token in Defuse. Instead of signing one `TokenDiff{diff: {token_A: -1_000_000, token_B: +X}}` (which would owe `protocol_fee.fee_ceil(1_000_000)` on `token_A`), the attacker signs `1_000_000` separate `TokenDiff` intents, each `{token_A: -1, token_B: +x}` (with `x = X/1_000_000` or matched via a second attacker-controlled account), and batches them all in one `MultiPayload` array passed to `execute_intents`/`simulate_intents`. Each call to `TokenDiff::execute_intent` sees `amount == 1`, hits the `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO` branch, and contributes `0` to `fees_collected`. All balance changes still net to zero across the batch via `TransferMatcher::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs:267-283`) as long as the attacker supplies matching counter-deltas (their own second account, or a real counterparty), since fee amounts flow through `internal_add_balance`/`internal_sub_balance` and are tracked in the same `TransferMatcher` as ordinary deltas.

No existing guard catches this: `MultiPayload::verify`, nonce/salt checks, and `TransferMatcher::finalize`'s zero-sum invariant all pass normally — they only ensure balances net to zero, not that fees are charged consistently regardless of how a swap is split into intents.

### Impact Explanation
Protocol fee revenue on NEP-245/IMT (multi-token/interoperable-multi-token) swaps is systematically bypassed for any signer willing to split their `TokenDiff` into unit-magnitude chunks. This falls under the explicitly listed Critical category "protocol fees bypassed." It does not move victim funds without authorization, but it under-collects fees owed to `fee_collector` on every NEP-245/IMT-denominated swap, repeatable by any unprivileged account, on any NEP-245/IMT token, for arbitrarily large amounts limited only by gas/batch size (out of scope per the prompt, but irrelevant to whether the bypass mechanism itself is real).

### Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's reach: hold an NEP-245/IMT balance in Defuse, have a nonzero protocol fee configured (default operating condition), and sign/batch `N` intents (or split across multiple `execute_intents` transactions) with a self-controlled counterparty account to satisfy the invariant. No role holder, relayer key, or victim key is needed. The only real friction is gas cost per intent, which the prompt explicitly places out of scope for judging the bypass mechanism.

### Recommendation
Compute the fee-exemption threshold on the signer's net aggregate delta per token across the whole intent/batch (or disallow multiple `TokenDiff` intents from the same signer touching the same NEP-245/IMT token within one execution, or accumulate per-token per-signer negative deltas across the entire `execute_signed_intents` call before applying the `amount <= 1` fee waiver) rather than per individual `TokenDiff::execute_intent` invocation.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or `tests/src/tests/defuse/intents/token_diff.rs` with sandbox):
1. Deploy Defuse with nonzero `Pips` fee (e.g. `Pips::ONE_PERCENT`) and an NEP-245 token; deposit `1_000_000` units of it to attacker account `A`, plus a matching NEP-141/other token to counterparty account `B` (attacker-controlled) to satisfy netting.
2. Path 1 ("single"): sign one `TokenDiff{A: {token_mt: -1_000_000, token_other: +closure}}` and a matching `TokenDiff` from `B`; execute; record `fee_collector`'s `mt_balance_of(token_mt)`.
3. Path 2 ("split"): reset state; sign `1_000_000` `TokenDiff` intents each `{A: {token_mt: -1, token_other: +x}}` plus matching `B` counter-intents; batch into one `execute_intents` call (or `simulate_intents` first to confirm no `InvariantViolated`); execute; record `fee_collector`'s `mt_balance_of(token_mt)`.
4. Assert `fee_from_path_1 == protocol_fee.fee_ceil(1_000_000)` and `fee_from_path_2 == 0`, demonstrating `fee_from_path_1 != fee_from_path_2` — confirming the fee bypass.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L70-72)
```rust
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-216)
```rust
    #[inline]
    pub fn token_fee(token_id: impl Into<TokenIdType>, amount: u128, fee: Pips) -> Pips {
        let token_id = token_id.into();
        match token_id {
            TokenIdType::Nep141 => {}
            TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
            // do not take fees on NFTs and MTs with |delta| <= 1
            TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
        }
        fee
    }
```
