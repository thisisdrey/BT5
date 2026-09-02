### No vulnerability found for this question.

**Rationale:** The behavior described is explicit, intended, and documented in the source itself. `TokenDiff::token_fee` contains an inline comment stating "do not take fees on NFTs and MTs with |delta| <= 1" [1](#0-0) . This exemption applies uniformly to `TokenIdType::Nep171` (true NFTs, always amount=1) and to `TokenIdType::Nep245`/`Imt` only when `amount <= 1`, while `amount > 1` on those same types always pays the full `fee` [2](#0-1) .

The "binding" the question wants broken — `fee credited == protocol_fee * value moved` — was never actually the protocol's fee model to begin with. Fees in `token_fee`/`fee_ceil` are computed strictly as a function of `amount` (unit count), not of any USD/economic "value" per unit, which the protocol has no way to know or enforce on-chain for an NFT/MT `TokenId` [3](#0-2) . An attacker who deploys their own NEP-245 contract can already choose arbitrary decimals/unit granularity for an NEP-141 token too (e.g., mint a token with 0 decimals representing a large nominal value) — the amount-based fee model treats "amount" as the unit of account for fee purposes by design, and this is consistent and intentional across the codebase, not a divergence introduced by a bug.

Because the exemption is a designed, always-applied branch (not a bypass of an otherwise-enforced fee), there is no case where `fees_collected` falls short of the fee actually "owed" under the protocol's own definition — the protocol defines the fee owed for `Nep245`/`Imt`/`Nep171` with `amount <= 1` as exactly `Pips::ZERO`. Comparing `delta=-1` vs `delta=-2` fees on the same MT token is expected and intended asymmetry (dust/whole-unit exemption for semi-fungible/collectible tokens), not evidence of fee circumvention against a broken invariant. This matches the question's own fallback: "documenting the exemption as intended so it is not exploitable beyond dust."

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L70-78)
```rust
            if *delta < 0 {
                let amount = delta.unsigned_abs();
                let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);

                // collect fee
                fees_collected
                    .add(token_id.clone(), fee)
                    .ok_or(DefuseError::BalanceOverflow)?;
            }
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L206-217)
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
}
```
