### Title
`TokenDiff::token_fee` exempts NEP-245/NFT/IMT transfers with `|delta| ≤ 1` per-intent, allowing fee bypass by splitting a large trade into many amount=1 `TokenDiff` intents - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` returns `Pips::ZERO` for `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt` whenever the *per-intent* `amount <= 1` [1](#0-0) . Because the fee is computed independently for each `TokenDiff` intent from that intent's own delta rather than from any aggregate of the signer's volume in the batch, an attacker can split one large NEP-245 (fungible-like) transfer into many separate `TokenDiff` intents each moving exactly `1` unit, causing `token_fee` to return zero for every leg and the total protocol fee collected to be `0` regardless of the aggregate value moved.

### Finding Description
The binding claimed by the protocol is: `fee_collected == fee * aggregate_economic_value_transferred` for a token-in leg. The actual code computes fee per intent:

```
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
``` [2](#0-1) 

and `token_fee` special-cases `Nep171 | Nep245 | Imt` to `Pips::ZERO` whenever that single intent's `amount <= 1`:
```rust
match token_id {
    TokenIdType::Nep141 => {}
    TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
    // do not take fees on NFTs and MTs with |delta| <= 1
    TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
}
fee
``` [1](#0-0) 

This exemption exists to avoid `fee_ceil` rounding a fee of 1 unit up to 100% for genuinely-indivisible NFT trades (`fee_ceil` rounds up: `ceil(amount * pips / MAX)` which for `amount=1` and any nonzero `fee` yields `1`, i.e., a 100% fee) [3](#0-2) . However, NEP-245 tokens (and IMTs) are not necessarily NFT-like; they can represent large, fungible-supply balances. The exemption is keyed only on the per-intent `amount`, with no aggregation across intents or accounting for the token's true fungibility. `TokenDiff::execute_intent` iterates only over the deltas *within a single intent* and fee is taken independently per intent with no cross-intent state [4](#0-3) .

The `TransferMatcher`/`Deltas` layer that later reconciles all deltas in the batch into concrete `Transfers` only nets balances to zero across the whole batch — it does not affect or re-derive fees; fee collection already happened per intent before `finalize()` runs [5](#0-4) [6](#0-5) .

Exploit: an attacker (as both sides of a matched trade, or as a taker paired with a counterparty/solver within the same batch) submits `N` `TokenDiff` intents in one `MultiPayload`/batch, each of the form `{Nep245Token: -1, OtherToken: +k}`. For every intent, `amount = 1` on the negative (token-in) leg, so `token_fee` returns `Pips::ZERO` and `fee_ceil(1) = 0`. Summed over `N` intents, the aggregate NEP-245 volume moved is `N`, yet total `fees_collected` deposited to `fee_collector` is `0` [7](#0-6) . A single equivalent `TokenDiff` moving `N` units in one intent would have incurred `fee_ceil(N, fee) = ceil(N * fee_pips / Pips::MAX)`, a materially nonzero amount for large `N`. No existing guard (`verify`, nonce checks, `TransferMatcher::finalize`, `checked_*` arithmetic) prevents this because none of them recompute or aggregate fees across intents — fee computation is strictly local to each intent's own delta.

### Impact Explanation
Value that should have gone to `fee_collector` never leaves the trading parties' balances — this is a direct under-collection of protocol fees, matching the Critical category "protocol fees bypassed or over-collected." The attacker (or any two colluding/self-controlled accounts) can convert an arbitrarily large trade in a fungible-semantics NEP-245/IMT token into zero-fee execution by chunking it into unit-size legs, at the cost of needing many intents in the batch (bounded by batch/gas limits, but each additional chunk incurs no per-unit fee). This is repeatable for every trade involving a NEP-245/IMT (or NEP-171) token as the "token_in" leg, on any account, any batch, indefinitely.

### Likelihood Explanation
No special privilege is required — any unprivileged signer can construct arbitrarily many `TokenDiff` intents with delta magnitude 1 and submit them via `execute_intents`/`simulate_intents` in one or more `MultiPayload`s. The only cost is the additional signing/serialization/gas overhead of issuing many small intents instead of one large one, which scales with `N` but does not by itself prevent the fee bypass for any given `N` that fits in a NEAR transaction's gas budget. The economic incentive to split increases with the fee percentage and the trade size, making this practical whenever `fee_ceil(N, fee)` on a single large trade would exceed the marginal transaction cost of issuing many chunks.

### Recommendation
Do not key the fee exemption purely on a single intent's local delta magnitude. Options: (1) determine token "NFT-ness" from the `TokenId`'s type/schema (e.g., a real NEP-171 or a NEP-245 token whose total supply/decimals indicate indivisibility) rather than from the transient per-intent `amount`; (2) if the exemption must remain amount-based, aggregate the signer's total negative delta for a given `token_id` across the whole `MultiPayload`/batch before deciding whether to apply the `amount <= 1` exemption, so that chunking cannot reduce the effective amount seen by `token_fee`; (3) alternatively, replace the ceiling-rounding special case with a fee computed using rounding that doesn't produce a 100% fee on `amount = 1` for truly fungible tokens (e.g., floor rounding with a minimum fee of 0 rather than an unconditional exemption).

### Proof of Concept
```
// tests/src/tests/defuse/intents/token_diff.rs (illustrative)
#[tokio::test]
async fn fee_bypass_by_chunking_nep245() {
    // setup: attacker account, a Nep245 token with large fungible balance,
    // fee_collector, protocol fee = Pips::ONE_PERCENT (or any nonzero fee)

    // Case A: single TokenDiff intent moving amount = 10_000 of the Nep245 token
    // expected: fee_collector balance increases by fee_ceil(10_000, fee) > 0
    let single_fee = submit_single_token_diff(nep245_token.clone(), -10_000, other_token.clone(), 10_000).await;
    assert!(single_fee > 0);

    // Case B: 10_000 separate TokenDiff intents, each moving amount = 1
    // of the same Nep245 token, submitted in one batch
    let chunked_fee = submit_many_token_diffs(nep245_token, -1, other_token, 1, /*count=*/10_000).await;

    // Binding under test: fee_collected == fee * aggregate_value_transferred
    // Both cases move the same aggregate volume (10_000 units), so fees should match.
    assert_eq!(chunked_fee, 0);
    assert_ne!(chunked_fee, single_fee); // demonstrates the bypass
}
```
This test asserts `fee_collector`'s balance change is `0` for the chunked batch despite an aggregate transferred volume identical to the single-intent case, which would have incurred `fee_ceil(10_000, fee)`.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L41-79)
```rust
impl ExecutableIntent for TokenDiff {
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        if self.diff.is_empty() {
            return Err(DefuseError::InvalidIntent);
        }

        let protocol_fee = engine.state.fee();
        let mut fees_collected: Amounts = Amounts::default();

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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L43-45)
```rust
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        self.deltas.finalize()
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
