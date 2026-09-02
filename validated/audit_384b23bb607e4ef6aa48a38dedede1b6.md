### No vulnerability found for this question.

**Verification of the claims:**

1. **Bundling has no effect on FT fee** — confirmed. `TokenDiff::execute_intent` iterates `self.diff` per `(token_id, delta)` pair and calls `Self::token_fee(token_id, amount, protocol_fee)` independently for each token, so an NFT leg's presence in the same diff cannot alter the fee computed for a co-bundled Nep141 leg [1](#0-0) . Same independence holds in the closure math (`supply_delta`/`closure_supply_delta`), which call `token_fee` per `token_id` with no cross-token state [2](#0-1) .

2. **`TokenIdType::Nep171` is fee-exempt by explicit design**, not an oversight — the code comment states this directly: "do not take fees on NFTs and MTs with |delta| <= 1" [3](#0-2) .

3. **Fee assessment is on notional per-token amount, not appraised USD/price-equivalent value** — also confirmed, and true, but this is inherent to how `TokenDiff` intents work: users freely negotiate arbitrary token deltas for arbitrary tokens with no on-chain oracle or valuation step anywhere in `execute_intent`, `closure_deltas`, or `TransferMatcher`. There is no binding in the codebase claiming fees are assessed on "true economic value" — fee assessment is explicitly defined as `Pips::fee_ceil` over the *token amount* of the negative delta, which is exactly what the code does [4](#0-3) .

**Why this is not an exploit:** The scenario requires the signer to voluntarily construct their own `TokenDiff` containing both their NFT and FT legs, and to accept an off-chain side payment for the difference. This is the signer's own asset, moved with the signer's own valid signature; no balance invariant is violated (deltas still net to zero across the batch via `TransferMatcher::finalize`), no unauthorized party's funds move, and no signature/nonce/lock check is bypassed. The "victim" here is only the protocol's own fee revenue on a fee category (`Nep171`) that the contract authors explicitly chose to exempt. This is a fee-schedule/business-logic design choice (arguably a revenue-optimization technique available to *any* legitimate trader, not a security bypass), and does not meet the required Critical/High impact bar of "value leaving the Verifier that the signer never authorised" — the signer here is the same party whose value is allegedly being "shifted." It is also explicitly listed as out of scope under "best-practice notes" / "theoretical findings" since no invariant equality is actually broken by bundling (as the question itself concedes), and the remaining claim ("value is never priced in fee terms") is a known, intentional protocol property rather than a defect introduced by `TokenDiff::closure` or `token_fee`.

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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L169-204)
```rust
    fn supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        if delta < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            delta.checked_mul_div_ceil(
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
                Pips::MAX.as_pips().into(),
            )
        } else {
            // token_out
            Some(delta)
        }
    }

    /// Returns closure for total supply delta that should be given in
    /// a single [`TokenDiff`] to successfully execute [`TokenDiff`] with
    /// given `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_supply_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        let closure = delta.checked_neg()?;
        if closure < 0 {
            // fee is taken only on negative deltas (i.e. token_in)
            closure.checked_mul_div_euclid(
                Pips::MAX.as_pips().into(),
                Self::token_fee(token_id, delta.unsigned_abs(), fee)
                    .invert()
                    .as_pips()
                    .into(),
            )
        } else {
            // token_out
            Some(closure)
        }
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
