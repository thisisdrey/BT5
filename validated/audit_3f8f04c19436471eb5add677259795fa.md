## Title
Protocol fee bypass on NEP-245/IMT token transfers via unit-delta splitting - ([File: contracts/defuse/core/src/intents/token_diff.rs])

## Summary
`TokenDiff::token_fee` waives fees on `Nep245`/`Imt` tokens whenever the per-intent `|delta| <= 1`, and this exemption is evaluated independently for every `TokenDiff` intent, with no aggregation across intents in the same signed payload or batch. Because a single `DefusePayload` can carry an arbitrary `Vec<Intent>` under one nonce/signature, a user with a balance of `N` units of a single NEP-245/IMT `token_id` can replace one `TokenDiff{diff: {T: -N, ...}}` intent with `N` separate `TokenDiff{diff: {T: -1, ...}}` intents, each paying `Pips::ZERO` fee, fully bypassing the fee that would be owed on the aggregate transfer.

## Finding Description
The broken binding: the protocol intends `Σ fees collected on token T across a batch == fee_ceil(Σ |negative deltas of T|, protocol_fee)`. In practice, for `Nep245`/`Imt` tokens:

```
Σ_{i=1}^{N} TokenDiff::token_fee(T, 1, fee).fee_ceil(1) == 0
fee_ceil(N, fee) > 0  (for fee > 0, N large enough)
```

The exemption logic lives in `TokenDiff::token_fee`: [1](#0-0) 

and it is applied per `TokenDiff::execute_intent` call, computing `fees_collected` purely from the current instance's own `self.diff`, with no cross-intent or cross-payload aggregation: [2](#0-1) 

A single signed `DefusePayload` can bundle an arbitrary number of `Intent`s (including many `TokenDiff`s) under one nonce, executed sequentially with no fee accumulation across them: [3](#0-2) [4](#0-3) 

Attacker flow: hold a single NEP-245 (or IMT) balance of `N` units under `token_id T`. Instead of signing one `TokenDiff{diff: {T: -N, U: +X}}`, sign one payload containing `N` `TokenDiff` intents, each `{T: -1, U: +x}` (with a matching counterparty leg, e.g. via a solver or self-matched deltas), so each call to `TokenDiff::token_fee(T, 1, fee)` hits the `amount > 1` branch's `else` and returns `Pips::ZERO`. Summed across the payload, `N` units of `T` are moved with zero fee collected, whereas the same aggregate movement expressed as one intent would owe `fee_ceil(N, fee)`.

No existing guard prevents this: `execute_signed_intent`/`Engine::execute_signed_intents` do not track per-token cumulative deltas for fee purposes across intents in a payload or across payloads in a batch; nonce/signature verification, deadline checks, and the invariant/`TransferMatcher::finalize` check only that debits and credits net to zero overall—they do not validate that fees were charged proportionally to the true aggregate transfer size.

## Impact Explanation
The `fee_collector` receives strictly less than the protocol-mandated fee (down to zero) on any NEP-245/IMT-denominated swap or transfer that the signer chooses to fragment into unit-sized `TokenDiff` legs within a single signed payload. This is repeatable without limit across accounts, tokens, and batches, and requires no privileged role—any signer holding an NEP-245/IMT balance can do it on every trade. This matches the explicitly listed Critical impact category "protocol fees bypassed."

## Likelihood Explanation
Preconditions are trivial: attacker needs a NEP-245/IMT balance in Defuse (or any counterparty willing to match unit deltas, e.g. a colluding/attacker-controlled solver) and protocol_fee > 0. The only cost is constructing a payload with `N` `TokenDiff` intents instead of one, all under a single signature/nonce — no extra signatures or nonces are required. This is fully feasible and highly likely to be exploited by any fee-sensitive market participant trading NEP-245/IMT assets.

## Recommendation
Aggregate fee computation per `token_id` across all `TokenDiff` intents within a single execution context (at minimum across a `DefuseIntents`/payload, ideally across the whole `execute_intents` batch) before applying the `amount <= 1` exemption, rather than evaluating the exemption per individual intent. Alternatively, restrict the exemption to token types that are provably non-fungible (e.g., NEP-171) and require an explicit, verified "NFT-like" flag or metadata check for NEP-245/IMT sub-token ids rather than relying solely on `amount <= 1` of a single intent.

## Proof of Concept
`cargo test` in `tests/src/tests/defuse/intents/token_diff.rs` style:
1. Deploy Defuse with `fee = Pips::ONE_PERCENT` (or any `fee > 0`), a NEP-245 MT contract, deposit `N = 100` units of `token_id T` to `user1`, and matching liquidity to a `solver`.
2. Case A: `user1` signs one payload with one `TokenDiff{diff: {T: -100, U: +X}}`; `solver` signs the closing leg. Execute; assert `fees_collected` for `T` on the fee_collector balance equals `TokenDiff::token_fee(T, 100, fee).fee_ceil(100) > 0`.
3. Case B: `user1` signs one payload containing 100 `TokenDiff` intents, each `{T: -1, U: +x}` (summing to the same aggregate `-100`/`+X`), matched by 100 corresponding solver legs. Execute; assert fee_collector's `T` balance increase is `0`.
4. Assert Case A's collected fee `> 0` while Case B's collected fee `== 0` for the same aggregate `-100` transfer of `T`, and that the buyer/seller economics (net tokens received) are otherwise equivalent — demonstrating the divergence and fee bypass.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-79)
```rust
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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
}
```

**File:** contracts/defuse/core/src/intents/mod.rs (L97-113)
```rust
impl ExecutableIntent for DefuseIntents {
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
        for intent in self.intents {
            intent.execute_intent(signer_id, engine, intent_hash)?;
        }
        Ok(())
    }
}
```
