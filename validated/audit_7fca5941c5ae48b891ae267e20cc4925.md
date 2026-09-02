### Title
Protocol fee bypass on NEP-245/IMT `TokenDiff` legs by splitting a transfer into multiple ≤1-unit intents - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes and collects the protocol fee independently per `Intent::TokenDiff` entry, and `TokenDiff::token_fee` returns `Pips::ZERO` for `Nep245`/`Imt` token legs whenever `amount <= 1`. Because a single signed `MultiPayload` (`DefuseIntents.intents: Vec<Intent>`) can carry an arbitrary number of `TokenDiff` intents under one signature/nonce, a signer can replace one `-1000` leg with 1000 separate `TokenDiff` intents each carrying `delta == -1` for the same `Nep245TokenId`, making every leg fee-exempt and reducing the aggregate protocol fee to zero for the same net token movement.

### Finding Description
Broken binding: `sum_over_legs(Pips::fee_ceil(protocol_fee, |delta_i|)) == Pips::fee_ceil(protocol_fee, |sum(delta_i)|)` for legs on the same `Nep245TokenId` within one settlement. This does not hold because fee computation is per-`TokenDiff`-intent, not per aggregate token movement.

Code path:
- `Engine::execute_signed_intent` (contracts/defuse/core/src/engine/mod.rs:42-83) verifies the signature/nonce **once per `MultiPayload`**, then calls `intents.execute_intent(...)` which iterates `DefuseIntents.intents: Vec<Intent>` (contracts/defuse/core/src/intents/mod.rs:30-37, 97-113) and executes each `Intent::TokenDiff` independently.
- `TokenDiff::execute_intent` (contracts/defuse/core/src/intents/token_diff.rs:41-104) computes `fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` per intent, where `amount = delta.unsigned_abs()` **of that single intent's leg**, not of the cumulative diff across intents in the payload/batch.
- `TokenDiff::token_fee` (contracts/defuse/core/src/intents/token_diff.rs:206-216) explicitly zeroes the fee for `Nep245`/`Imt` when `amount <= 1`:
```
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
```
- Balances are still merged correctly per-token via `TransferMatcher`/`TokenTransferMatcher` in `finalize()` (contracts/defuse/core/src/engine/state/deltas.rs:242-392), which only cares that deltas across the whole batch net to zero per `TokenId` — it does not re-derive or enforce the fee that should have applied to the aggregate negative delta.

Exploit flow: attacker A signs one `MultiPayload` containing 1000 `TokenDiff` intents, each `{diff: {nep245_token: -1}}`, and a counterparty (A's own second account, or a colluding/complicit party) contributes a matching `+1000` (or split) positive delta for the same `Nep245TokenId` in the same batch so `TransferMatcher::finalize` succeeds (contracts/defuse/core/src/engine/state/deltas.rs:265-283, matches `test_unmatched`/`invariant_violated` behavior at tests/src/tests/defuse/intents/token_diff.rs:277-373 confirming unmatched per-token deltas abort the whole batch — so a counterparty for the *same token* is a strict precondition, not merely convenient). Each of A's 1000 legs independently hits `TokenIdType::Nep245 ... amount <= 1 => Pips::ZERO`, so `fees_collected` stays empty for all of them, and `internal_add_balance(fee_collector, fees_collected)` at contracts/defuse/core/src/intents/token_diff.rs:97-101 is skipped/credits 0. Had A instead sent a single `TokenDiff{diff: {nep245_token: -1000}}`, `token_fee` would hit the `amount > 1` branch and charge `protocol_fee`, and `fee_ceil(1000)` would be nonzero and credited to `fee_collector`.

Existing guards do not prevent this: `MultiPayload::verify`, nonce, and `TransferMatcher::finalize` all operate correctly and are not designed to detect fee-avoidant restructuring; they only enforce authenticity and balance conservation, not fee correctness across split legs.

### Impact Explanation
The `fee_collector` is under-credited relative to the fee that would be due on the equivalent aggregate NEP-245/IMT transfer, while the signer benefits by paying zero fee instead of `Pips::fee_ceil(protocol_fee, N)`. This is repeatable per NEP-245/IMT token, per batch, with no bound on how many times an attacker can split a transfer, and requires only depositing/holding a NEP-245 (multi-token) balance and a second account (or willing counterparty) to satisfy the per-token netting requirement. This matches the "protocol fees bypassed" Critical category.

### Likelihood Explanation
Preconditions are modest: the attacker needs a NEP-245 balance ≥ N units, `protocol_fee > 0`, and a matching counter-leg (which the attacker can supply from a second self-controlled account, since `TransferMatcher` only requires netting per token across the batch, not a genuine independent trading partner). Cost is simply constructing N `TokenDiff` intents in one `DefuseIntents.intents` vector under a single signature — no extra signatures, nonces, or NEAR fees beyond gas for a larger call. This is a straightforward, mechanically reproducible bypass, limited only by NEP-245/IMT tokens (not NEP-141), which somewhat narrows blast radius but doesn't eliminate it.

### Recommendation
Compute and charge the protocol fee on the **aggregate** negative delta per `TokenId` across the whole settlement batch (i.e., after all intents in the `MultiPayload`/batch are processed, or by accumulating per-token negative deltas before applying `token_fee`), rather than per individual `TokenDiff` intent. Alternatively, remove or tighten the `amount <= 1` fee exemption for `Nep245`/`Imt` so it cannot be trivially defeated by splitting a single logical transfer into many unit legs within one signed payload.

### Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (new #[test] in mod tests)
#[test]
fn split_legs_bypass_fee_on_nep245() {
    let token_id = TokenId::from(Nep245TokenId::new(
        "mt.near".parse::<AccountId>().unwrap(), "ft1".to_string(),
    ));
    let fee = Pips::ONE_PERCENT;

    // Single intent: -1000 in one leg
    let single_fee = TokenDiff::token_fee(&token_id, 1000, fee).fee_ceil(1000);
    assert!(single_fee > 0, "single leg should be charged a fee");

    // Split into 1000 legs of -1 each (as separate TokenDiff intents)
    let split_fee_total: u128 = (0..1000)
        .map(|_| TokenDiff::token_fee(&token_id, 1, fee).fee_ceil(1))
        .sum();
    assert_eq!(split_fee_total, 0, "split legs collect zero fee, bypassing protocol_fee");

    // Binding broken: aggregate fee for same net token movement differs
    assert_ne!(single_fee, split_fee_total);
}
```
For an end-to-end sandbox proof, extend `tests/src/tests/defuse/intents/token_diff.rs` to sign one `MultiPayload` from `user1` with 1000 `TokenDiff` intents `{nep245_token: -1}` matched against `user2`'s single `TokenDiff{nep245_token: +1000}`, execute via `execute_intents`, and assert `fee_collector`'s `mt_balance_of(nep245_token)` is `0`, compared against a control run using a single `TokenDiff{nep245_token: -1000}` intent where `fee_collector` receives `Pips::fee_ceil(fee, 1000) > 0`.