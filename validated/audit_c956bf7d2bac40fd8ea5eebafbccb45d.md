### Title
Fee-splitting via multiple 1-unit `TokenDiff` intents bypasses protocol fees on Nep245/Imt tokens - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` (contracts/defuse/core/src/intents/token_diff.rs:206-217) returns `Pips::ZERO` whenever a `Nep245`/`Imt` token's per-intent `|delta| <= 1`, and fees are computed independently inside each `TokenDiff::execute_intent` call over that intent's own `diff` map. Since `execute_signed_intents` (contracts/defuse/core/src/engine/mod.rs:32-40) processes each `MultiPayload`/intent's fee calculation in isolation with no cross-intent aggregation of the same `(signer, token_id)` pair, an attacker can split a large semi-fungible transfer into N separate signed `TokenDiff` intents of `delta = ±1` each, so every individual fee computation hits `amount <= 1` and returns `Pips::ZERO`, while doing the identical net transfer of N units via one intent with `delta = ±N` would trigger `token_fee` returning the nonzero `fee` and be charged a positive fee via `Pips::fee_ceil`.

### Finding Description
The broken binding: `fee_owed(aggregate_delta = N) == sum(fee_owed(delta_i)) for any partition of N into delta_i)`. This does not hold for `TokenIdType::Nep245`/`Imt` because:

```rust
// contracts/defuse/core/src/intents/token_diff.rs:206-217
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

`token_fee` is invoked once per `TokenDiff` intent execution using that single intent's `delta.unsigned_abs()` (line 71-72: `let amount = delta.unsigned_abs(); let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);`). Because each `TokenDiff::execute_intent` call is independent — a fresh `fees_collected: Amounts` accumulator local to that call (line 57) — there is no state anywhere (in `Deltas`, `TransferMatcher`, or `Engine::execute_signed_intents`) that aggregates the total delta moved for the same token across multiple intents/payloads in a batch before computing the fee threshold. `execute_signed_intents` (engine/mod.rs:32-40) simply loops over each signed payload and calls `execute_signed_intent`, which independently calls `intents.execute_intent(...)`.

Attack construction: the attacker (an unprivileged signer) crafts N `MultiPayload`s (or N `TokenDiff` intents within a batch, each with its own valid nonce signed by the attacker's own key) each containing a `TokenDiff{diff: {token_id: -1}}` matched by a counter-intent (their own or a counterparty's) `{token_id: +1}` for the same `Nep245`/`Imt` `token_id`. Each of these N intents individually satisfies `amount == 1`, so `token_fee` returns `Pips::ZERO` and `fee_ceil(1) == 0` for every one of them. The `TransferMatcher`/`finalize()` step (contracts/defuse/core/src/engine/state/deltas.rs) only nets balances into `Transfers` for settlement — it does not recompute or reconcile any fee, so the zero-fee outcome from each intent execution stands. The aggregate transferred amount is N units for zero total protocol fee, whereas a single `TokenDiff{diff: {token_id: -N}}` intent would compute `token_fee(..., N, fee)` with `amount > 1`, returning the real `fee`, and `fee_ceil(N)` would be nonzero.

No existing guard prevents this: `MultiPayload::verify`, nonce/salt checks, and `Lock` checks only guard authenticity and replay, not fee aggregation; `TransferMatcher::finalize` only guarantees balance conservation across the batch, not fee conservation.

### Impact Explanation
Protocol fees intended to be collected on negative deltas (token_in) of semi-fungible (`Nep245`/`Imt`) token transfers can be reduced to zero for any aggregate volume by partitioning the transfer into unit-sized (`|delta|<=1`) `TokenDiff` intents, each independently signed by the party paying the fee. This directly matches the "protocol fees bypassed" Critical impact category: the fee collector (`fee_collector` account, contracts/defuse/core/src/engine/state/mod.rs / contracts/defuse/src/fees.rs) receives strictly less than it is owed under the documented fee model, for any signer who chooses to fragment their trades. This is repeatable per account, per token, and per batch size N with no bound other than gas/transaction-size limits (explicitly out of scope per the rules, but does not block the fee-bypass mechanism itself, only its scale in one transaction — it is trivially repeatable across multiple transactions).

### Likelihood Explanation
The attacker needs no special privileges — only their own signing key and normal `execute_intents`/`simulate_intents` access, exactly as permitted under the stated threat model. They need a `Nep245`/`Imt` balance (or a counterparty willing to swap) which they fully control since they can deploy/control their own MT/FT/receiver contracts and mint/hold their own balances in the Verifier. Constructing N `TokenDiff` intents each with `delta = ±1` on the same `token_id` is straightforward and requires no interaction with any privileged role, upgrader, or relayer key. The only cost is transaction/gas overhead for N intents, which is a routine cost, not a blocking precondition.

### Recommendation
Compute and apply the fee-exemption threshold on the aggregate delta actually transferred per `(signer, token_id)` across the whole batch/transaction rather than per individual `TokenDiff` intent. For example, accumulate per-token deltas across all intents executed within a single `execute_signed_intents` call (or across the whole `Deltas`/`TransferMatcher` state) before evaluating `token_fee`, so that `amount` reflects the true net negative delta for that token by that signer in the batch, closing the unit-fragmentation loophole. Alternatively, remove the `amount <= 1` exemption for `Nep245`/`Imt` entirely and rely on a flat per-unit fee, or track cumulative small-amount transfers per signer/token across a rolling window to prevent fee-free fragmentation.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (add to #[cfg(test)] mod tests)
#[test]
fn fee_bypass_via_unit_fragmentation() {
    let token_id: TokenId = Nep245TokenId::new("mt.near".parse().unwrap(), "semi_fungible".to_string()).into();
    let fee = Pips::ONE_PERCENT; // nonzero fee

    // Single intent moving aggregate amount N=100 in one delta
    let n: u128 = 100;
    let single_fee = TokenDiff::token_fee(token_id.clone(), n, fee).fee_ceil(n);
    assert!(single_fee > 0, "aggregate single-intent transfer should pay nonzero fee");

    // N intents each moving delta=1 (fragmented)
    let mut fragmented_total_fee: u128 = 0;
    for _ in 0..n {
        let per_intent_fee = TokenDiff::token_fee(token_id.clone(), 1, fee).fee_ceil(1);
        fragmented_total_fee += per_intent_fee;
    }
    assert_eq!(fragmented_total_fee, 0, "fragmented per-unit intents collect zero fee");

    // Binding violated: aggregate fee owed (single_fee) > fee actually collected (fragmented_total_fee)
    assert!(single_fee > fragmented_total_fee);
}
```
This test directly demonstrates, using only public `TokenDiff` APIs, that splitting one `Nep245` transfer of amount `N` into `N` intents of `delta = 1` each collects strictly less total fee (`0`) than executing it as a single intent (`single_fee > 0`), confirming the fee-bypass. A full `near-workspaces` sandbox test can additionally submit `N` real signed `MultiPayload`s via `execute_intents` for the same signer/token and assert the resulting `fee_collector` balance is `0` versus nonzero when the same net transfer is submitted as one `TokenDiff` intent.