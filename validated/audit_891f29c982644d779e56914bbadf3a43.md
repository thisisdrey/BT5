## Title
Protocol fee bypass on NEP-245/IMT `TokenDiff` swaps via unit-splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

## Summary
`TokenDiff::execute_intent` computes the protocol fee independently for each executed intent, using only that intent's own `delta` magnitude to decide whether `Self::token_fee` should return `Pips::ZERO` for NEP-245/IMT tokens. Because that "no-fee" threshold (`amount <= 1`) is evaluated per intent rather than per signer's aggregate delta on the token within the batch, a signer can split one intended `-N` NEP-245 delta into `N` separate `TokenDiff` intents each with `delta == -1`, causing `fee_collector` to receive `0` instead of `Pips::fee_ceil(fee, N)`.

## Finding Description
The broken binding is: **fees credited to `fee_collector` for token `T` == `Pips::fee_ceil(protocol_fee, N)` over the aggregate negative delta of `T` for a given signer in the batch.**

In `TokenDiff::execute_intent` [1](#0-0) , for each `(token_id, delta)` pair in a single `TokenDiff.diff` map, the code computes:
```
let amount = delta.unsigned_abs();
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
and `Self::token_fee` explicitly zeroes the fee for `Nep245`/`Imt` (and `Nep171`) when `amount <= 1`: [2](#0-1) 

This threshold check is local to the single intent's own delta, not to the cumulative amount the signer moves on that `TokenId` across the whole `MultiPayload` batch. Because `DefuseIntents` can carry an arbitrary `Vec<Intent>` under a single signed payload/nonce, and `Engine::execute_signed_intents` only checks the *global* net-zero invariant across the whole batch via `TransferMatcher::finalize` at the very end [3](#0-2) , an attacker can:

1. Sign one payload for account `A` containing `N` `TokenDiff` intents, each `diff = { token_id: NEP245_X: -1 }`. Each call computes `fee = token_fee(NEP245_X, 1, protocol_fee).fee_ceil(1) == 0`, so `fees_collected` for that leg is `0`.
2. Have a colluding/self-owned counterparty account `B` sign a matching `TokenDiff` intent `diff = { token_id: NEP245_X: +N }` (positive deltas never incur fees, per the `if *delta < 0` guard, regardless of split).
3. Submit `[A_payload, B_payload]` to `execute_intents`/`simulate_intents`.

`A`'s total withdrawal registered in `TransferMatcher` is still `N` (sum of the `N` unit legs), and `B`'s deposit is `N`; `fee_collector`'s deposit is `0`. The batch nets to zero (`-N + N + 0 == 0`), so `TransferMatcher::finalize` [4](#0-3)  succeeds — no `InvariantViolated` error, no reverts, no guard rejects it. Had `A` instead signed a single `TokenDiff { NEP245_X: -N }`, the fee would be `Pips::fee_ceil(protocol_fee, N) > 0` (for `N > 1`, `protocol_fee > 0`), and `B` would need to supply only `N - fee` to balance the invariant, with the difference credited to `fee_collector` via `internal_add_balance` [5](#0-4) .

None of the existing guards catch this: `MultiPayload::verify`, nonce/salt checks, and `#[pause]` are orthogonal to fee computation; the only cross-cutting check is the balance invariant in `TransferMatcher`, which is satisfied precisely because the split changes both the fee taken and the compensating deposit in lockstep.

## Impact Explanation
`fee_collector` under-collects protocol fees on every NEP-245/IMT `TokenDiff` trade that a signer chooses to structure as unit-sized legs instead of one aggregate delta. This is repeatable per token, per batch, with no cap — an attacker (or two colluding attacker-controlled accounts) can move arbitrarily large NEP-245 balances through the Verifier while paying the protocol nothing, whereas a single equivalent `TokenDiff` would have paid `Pips::fee_ceil(protocol_fee, N)`. This matches the explicitly listed Critical category "protocol fees bypassed" — value that should flow to `fee_collector` never does, at scale, for any actor willing to split their diff into many intents.

## Likelihood Explanation
Preconditions are minimal and fully within an unprivileged attacker's control: `protocol_fee > 0` (any nonzero configured fee), the traded asset is `TokenId::Nep245` (or `Imt`), and the attacker needs custody of `N` units and one counterparty willing to provide the matching `+N` deposit (which can be the attacker's own second account — no third-party cooperation is required for a pure self-dealing/wash transfer disguised as a swap). Constructing `N` `TokenDiff` intents in one signed `DefuseIntents` payload is trivial and costs only the gas/transaction overhead of a larger `Vec<Intent>`; no special role, relayer key, or upgrade is needed.

## Recommendation
Compute `Self::token_fee`'s NEP-245/IMT "amount <= 1" exemption against the signer's cumulative negative delta per `TokenId` across the whole diff (and ideally across the whole batch/signer within `execute_signed_intents`), not per individual intent leg. Concretely, aggregate all `TokenDiff` intents' deltas per `(signer_id, token_id)` before applying the `token_fee` threshold check, or move the fee computation to operate on the total withdrawal amount already tracked by `TransferMatcher` for that signer/token instead of the single intent's raw delta.

## Proof of Concept
```rust
// cargo test (unit test in contracts/defuse/core, or near-workspaces sandbox test
// alongside tests/src/tests/defuse/intents/token_diff.rs)

// Setup: protocol_fee = Pips::ONE_PERCENT, token_id = TokenId::Nep245(mt_token),
// signer A custodies N = 100 units of mt_token.

// Case 1: single TokenDiff intent, delta = -100
let fee_single = TokenDiff::token_fee(&token_id, 100, protocol_fee).fee_ceil(100);
assert!(fee_single > 0);

// Case 2: 100 separate TokenDiff intents, each delta = -1
let fee_split_total: u128 = (0..100)
    .map(|_| TokenDiff::token_fee(&token_id, 1, protocol_fee).fee_ceil(1))
    .sum();
assert_eq!(fee_split_total, 0);

// Sandbox-level assertion: execute batch [A: 100x TokenDiff(-1), B: TokenDiff(+100)]
// via execute_intents, then read fee_collector's mt balance for token_id.
// Expect fee_collector balance == 0, vs. expected fee_single > 0 for the
// equivalent single -100 delta batch, demonstrating fee under-collection.
```

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
```rust
    pub fn execute_signed_intents(
        mut self,
        signed: impl IntoIterator<Item = MultiPayload>,
    ) -> Result<Transfers> {
        for signed in signed {
            self.execute_signed_intent(signed)?;
        }
        self.finalize()
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L267-283)
```rust
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
```
