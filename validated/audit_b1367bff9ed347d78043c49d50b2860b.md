### Title
`TokenDiff::token_fee` fee-exemption for `Nep245`/`Imt` at `amount <= 1` lets colluding signers move unbounded notional of a multi-token `token_id` at zero fee by splitting the swap into unit-sized diffs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::execute_intent` computes the protocol fee independently for each `(token_id, delta)` pair inside a single `TokenDiff::diff` map, using only that line's own `unsigned_abs()` magnitude, and `TokenDiff::token_fee` returns `Pips::ZERO` for `Nep245`/`Imt` whenever that per-line magnitude is `1`. Because the batch-wide transfer volume for a `token_id` is only computed afterwards by `TransferMatcher::finalize`, which nets withdrawals against deposits across every intent/signer in the whole `execute_intents` call, an attacker can move an arbitrarily large aggregate quantity of a semi-fungible `Nep245` `token_id` by splitting it into many `delta = ±1` `TokenDiff` legs (either in one `DefuseIntents.intents` array under a single signature, or across many signed `MultiPayload`s), causing every fee computation to see `amount == 1` and pay zero, while the real notional transferred can be arbitrarily large.

### Finding Description
The binding that should hold is:
`fee_collected(token T, batch) == Pips::fee_ceil(protocol_fee, net_transfer_volume(T, batch))`
where `net_transfer_volume(T, batch)` is the actual amount of `T` moved between accounts once all `TokenDiff` deltas in the batch are netted by `TransferMatcher::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs:267-283, 337-391`).

Instead, the code computes fee per intent-line, before that netting happens: [1](#0-0) 

and the fee schedule itself: [2](#0-1) 

`TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` falls through to the normal `fee`, but any `TokenDiff` line with `amount <= 1` (i.e. exactly `1`, since `0` is rejected) returns `Pips::ZERO` for `Nep171`/`Nep245`/`Imt`. This exemption exists because `Pips::fee_ceil(1)` rounds up to `1` for any nonzero fee rate (100% of a unit), which is punitive for true NFTs where `amount` is always `1`. But `Nep245` (and `Imt`) `token_id`s are semi-fungible — a single `token_id` can carry an arbitrarily large balance — so the exemption threshold (`amount <= 1`) is evaluated per `TokenDiff` **line**, not per aggregate volume moved for that `token_id` across the batch.

`TransferMatcher` (`contracts/defuse/core/src/engine/state/deltas.rs:241-284`) aggregates all withdrawals/deposits for a given `token_id` across *every* intent and *every* signer in the same `execute_signed_intents` call (`contracts/defuse/core/src/engine/mod.rs:32-40`), matching them without any knowledge of, or connection to, the per-line fee that was already assessed and (not) collected during `execute_intent`. There is no invariant tying fee collection to the net matched volume.

Exploit: two colluding, unprivileged accounts A and B each deposit balance of the same `Nep245` `token_id` (e.g., an MT "position" token with a large quantity). To move `N` units from A to B without paying fees:
- A signs one `MultiPayload` whose `DefuseIntents.intents` contains `N` separate `Intent::TokenDiff` entries, each `diff = {token_id: -1}` (or A signs `N` separate `MultiPayload`s, each with one such `TokenDiff`).
- B signs the mirror `N` entries, each `diff = {token_id: +1}` (plus whatever counter-asset B gives up elsewhere, priced the same way to avoid the exemption on that other token, or simply absorbed since `token_out` deltas never pay fees anyway per `supply_delta`).
- All these `MultiPayload`s are submitted together in one `execute_intents(signed: Vec<MultiPayload>)` call.
- Each individual `TokenDiff::execute_intent` call sees `delta.unsigned_abs() == 1` for that `token_id`, so `Self::token_fee(...)` returns `Pips::ZERO`, and `fees_collected` for `token_id` stays `0` on every single execution.
- `TransferMatcher::finalize` nets A's `N` withdrawals of `1` against B's `N` deposits of `1` into a single logical transfer of `N` units from A to B, satisfying the "everything must net to zero" invariant with no `InvariantViolated` error.
- Total fee credited to `fee_collector` for `token_id` is `0`, regardless of how large `N` is.

None of the existing guards prevent this: `MultiPayload::verify`, `has_public_key`, `verify_intent_nonce`, and nonce commitment only authenticate and de-duplicate each signed message; they say nothing about fee aggregation. `TransferMatcher::finalize` only checks that deltas net to zero, it has no fee logic. `TokenDiff::execute_intent` never looks at other intents in the batch or the resulting matched transfer size, only at its own line's `delta`.

### Impact Explanation
Protocol fees for `Nep245` (multi-token/semi-fungible) and `Imt` token classes are bypassed entirely for any counterparties willing to structure their trade as many unit-sized `TokenDiff` legs instead of one large delta. This directly matches the listed Critical category "protocol fees bypassed or over-collected": the `fee_collector` receives `0` regardless of the real notional value transferred between the colluding accounts, for as large an `N` as the transaction/gas/message-size limits of a single `execute_intents` call allow (splitting across multiple calls removes even that bound). This is repeatable indefinitely by any pair (or more) of unprivileged accounts holding balances of a shared `Nep245`/`Imt` `token_id`, and scales with however many unit legs they're willing to include.

### Likelihood Explanation
No privileged role, relayer key, or victim key is required — only two ordinary Verifier accounts that both hold (or one deposits into) balances of the same `Nep245`/`Imt` `token_id`, and are willing to sign `DefuseIntents` with many `TokenDiff` entries (cheap and fully client-side; NEP-413 signing does not require any on-chain interaction per entry, and a single `DefuseIntents.intents: Vec<Intent>` can hold arbitrarily many `TokenDiff` legs under one signature). The only limiting factors are transaction size/gas, which bound `N` per call but not across repeated calls. This is straightforward to demonstrate deterministically in a `near-workspaces` sandbox test with a fixed small `N` and a nonzero `protocol_fee`.

### Recommendation
Assess and collect the fee based on the net matched transfer volume per `token_id` across the whole batch (i.e., after `TransferMatcher::finalize` resolves deposits/withdrawals), not per individual `TokenDiff` line. Alternatively, remove or tighten the `amount <= 1` exemption for `Nep245`/`Imt` specifically (keep it only for genuinely-indivisible `Nep171` NFTs), or track and fee cumulative per-account-per-token negative deltas within a single `execute_intents` invocation before applying the `amount <= 1` threshold, so splitting into unit legs cannot zero out the fee on aggregate volume.

### Proof of Concept
`near-workspaces`/sandbox test (Rust, using the existing `defuse_sandbox` test harness in `tests/src/tests/defuse/intents/token_diff.rs`):
1. Set env fee to a nonzero `Pips` (e.g. `Pips::ONE_PERCENT`).
2. Create accounts `A` and `B`, deploy/register an MT contract producing a `Nep245TokenId` (`mt.near`, `"pos1"`), and deposit `N = 1000` units of that `token_id` into `A`'s Verifier balance, and enough of a second asset into `B` for the counter-leg (or simply `+1`/`-1` mirrored `token_id` legs between A and B with no second asset, since a pure `token_id` transfer only needs matching deltas).
3. Build a single `DefuseIntents` for `A` containing `1000` `Intent::TokenDiff` entries, each `diff = {token_id: -1}`, signed once via `sign_defuse_payload_default`.
4. Build the mirrored `DefuseIntents` for `B` containing `1000` `Intent::TokenDiff` entries, each `diff = {token_id: +1}`, signed once.
5. Call `env.defuse_simulate_and_execute_intents` with `[a_signed, b_signed]`.
6. Assert:
   - `simulate_intents` reports no `invariant_violated` (deltas net to zero).
   - Post-execution, `mt_balance_of(A, token_id) == initial_A - 1000` and `mt_balance_of(B, token_id) == initial_B + 1000` (full `1000` units moved).
   - `mt_balance_of(fee_collector, token_id) == 0` — i.e. `Pips::ONE_PERCENT.fee_ceil(1000) == 10` was **not** collected, violating the binding `fee_collected(token, batch) == Pips::fee_ceil(fee, net_transfer_volume(token, batch))`.
7. Contrast with a control test using a single `TokenDiff` with `diff = {token_id: -1000}` in one line, showing `fee_collected(token_id) == fee.fee_ceil(1000) > 0`, to prove the discrepancy is caused purely by splitting into unit legs.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L59-78)
```rust
        for (token_id, delta) in &self.diff {
            if *delta == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            // add delta to signer's account
            engine
                .state
                .internal_apply_deltas(signer_id, [(token_id.clone(), *delta)])?;

            // take fees only from negative deltas (i.e. token_in)
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
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
