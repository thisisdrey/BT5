Based on my research, the codebase (Kohvert/intents--025) is a NEAR Protocol intents/settlement system, not a Hyperliquid-style EVM bridge with L1/spot decimal conversions. I searched for the specific precision-loss bug class described in the report — a value being scaled down (losing precision) then scaled back up and used inconsistently across two different operations that must match (e.g., a vault withdrawal amount vs. a spot bridge amount) — within the in-scope directories.

Findings:
- The `defuse` core token accounting uses a single `u128` amount representation throughout (`internal_sub_balance`/`internal_add_balance`, `Amounts`), with no decimal-rescaling step between two different fixed-point representations of the same value [1](#0-0) .
- The only decimal/rounding-sensitive logic in scope is the `TokenDiff` fee computation, which uses `checked_mul_div`/`checked_mul_div_ceil` with an explicit `closure_delta`/`supply_delta` round-trip that is proven invariant-preserving by tests (`invariant_violated` for `delta + closure` summing to zero) [2](#0-1) .
- `Pips::fee`/`fee_ceil` perform a single proportional calculation without any two-step decimal conversion that could silently diverge like the HyperLiquid `_scaleToPerp

### Citations

**File:** contracts/defuse/src/contract/tokens/mod.rs (L76-117)
```rust
    pub(crate) fn withdraw(
        &mut self,
        owner_id: &AccountIdRef,
        token_amounts: impl IntoIterator<Item = (TokenId, u128)>,
        memo: Option<impl Into<String>>,
        force: bool,
    ) -> Result<()> {
        let owner = self
            .storage
            .accounts
            .get_mut(owner_id)
            .ok_or_else(|| DefuseError::AccountNotFound(owner_id.to_owned()))?
            .get_mut_maybe_forced(force)
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;

        let mut burn_event = MtBurnEvent {
            owner_id: Cow::Owned(owner_id.to_owned()),
            authorized_id: None,
            token_ids: Vec::new().into(),
            amounts: Vec::new().into(),
            memo: memo.map(Into::into).map(Into::into),
        };

        for (token_id, amount) in token_amounts {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            burn_event.token_ids.to_mut().push(token_id.to_string());
            burn_event.amounts.to_mut().push(amount);

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;

            self.storage
                .state
                .total_supplies
                .sub(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L139-217)
```rust
    pub fn closure_deltas(
        deltas: impl IntoIterator<Item = (TokenId, i128)>,
        fee: Pips,
    ) -> Option<TokenDeltas> {
        deltas
            .into_iter()
            // collect total supply deltas
            .try_fold(TokenDeltas::default(), |deltas, (token_id, delta)| {
                let supply_delta = Self::supply_delta(&token_id, delta, fee)?;
                deltas.with_apply_delta(token_id, supply_delta)
            })?
            .into_inner()
            .into_iter()
            // calculate closures from total supply deltas
            .try_fold(TokenDeltas::default(), |deltas, (token_id, delta)| {
                let closure = Self::closure_supply_delta(&token_id, delta, fee)?;
                deltas.with_apply_delta(token_id, closure)
            })
    }

    /// Returns closure for delta that should be given in a single
    /// [`TokenDiff`] to successfully execute [`TokenDiff`] with given
    /// `delta` on the same token assuming given `fee`.
    #[inline]
    pub fn closure_delta(token_id: &TokenId, delta: i128, fee: Pips) -> Option<i128> {
        Self::closure_supply_delta(token_id, Self::supply_delta(token_id, delta, fee)?, fee)
    }

    /// Returns total supply delta from token delta
    #[inline]
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
}
```
