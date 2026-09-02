## Title
Fee-Splitting Bypasses Protocol Fees on `Nep245`/`Imt` Token Swaps in `TokenDiff` — (File: `contracts/defuse/core/src/intents/token_diff.rs`)

## Summary
The `TokenDiff::token_fee` function exempts `Nep245` (multi-token) and `Imt` token deltas from protocol fees whenever the traded amount is `<= 1`, while charging the full protocol fee when the amount is `> 1`. Because this threshold is evaluated independently for every `TokenDiff` intent (and a signer can submit an arbitrary number of `TokenDiff` intents in a single signed batch), an attacker can split a large trade of such a token into many unit-sized trades and pay zero protocol fee in total on the entire transferred volume — the same class of bug as the reported royalty issue, where the fee/royalty owed depends on how a transaction is fragmented rather than on the total value moved. [1](#0-0) 

## Finding Description
`execute_intent` computes the protocol fee per `TokenDiff` intent, once per negative delta, using `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`: [2](#0-1) 

`token_fee` decides whether to charge the fee based solely on the magnitude of that single delta:
```rust
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
``` [3](#0-2) 

This exemption was designed for genuinely non-fungible transfers (a single unit of an NFT-like multi-token id). However, `Nep245`/`Imt` token ids can back fungible-like balances with large supply (e.g., semi-fungible or "IMT" balances). A signer can place any number of `TokenDiff` intents inside one `DefuseIntents.intents` list (or across multiple signed payloads in one `execute_intents` call): [4](#0-3) 

Each intent is executed independently through `intents.execute_intent(&signer_id, self, hash)`, so the "amount" seen by `token_fee` is always the per-intent delta, not the cumulative amount moved by the signer: [5](#0-4) 

By decomposing a swap of `N` units of a `Nep245`/`Imt` token into `N` separate `TokenDiff` intents, each moving exactly `1` unit (matched against a counterparty's equal-and-opposite intents in the same batch so the global invariant still balances to zero), every individual fee calculation hits the `amount <= 1` branch and returns `Pips::ZERO`. The equality that should hold — `fees_owed(total_amount_moved) == fees_collected` — is broken: fees owed on the true aggregate volume are non-zero (since `amount > 1` in aggregate), yet fees actually collected sum to zero because each unit-sized intent is evaluated in isolation.

This is the direct structural analog of the reported royalty-dilution bug: there, the amount of royalty owed depended on how many tokens were bundled into one listing; here, the amount of protocol fee owed depends on how many separate 1-unit `TokenDiff` intents a trade is fragmented into.

## Impact Explanation
This allows an attacker (or a solver/user pair colluding, or even a single signer swapping with themselves across two accounts) to move arbitrary volumes of `Nep245`/`Imt`-based tokens through the intents settlement engine while paying zero protocol fee, directly reducing the protocol's fee revenue on every such swap. Per the stated impact categories, this is a "fees bypassed" scenario — a Critical-class impact, since fee revenue that should be collected on token-in legs is fully avoidable at will.

## Likelihood Explanation
The exploit requires no special privileges: any two (or more) accounts able to sign `TokenDiff` intents can construct a batch where a large trade is split into many 1-unit legs. Since `execute_intents` accepts arbitrary `Vec<MultiPayload>` with any number of intents, and gas/complexity of adding more small `TokenDiff` entries scales linearly (not prohibitively), this is practically executable, limited mainly by gas costs for very large volumes, which is not a blocking cost for moderate-value trades.

## Recommendation
Compute and charge the `Nep245`/`Imt` fee based on the total (net) amount traded per token id per signer across the whole batch/payload (i.e., aggregate deltas for the same `token_id` before evaluating the `amount > 1` threshold), rather than evaluating the threshold independently per individual `TokenDiff` intent. Alternatively, remove the amount-based exemption entirely for `Nep245`/`Imt` and only exempt token ids that are provably non-fungible (e.g., via a per-token-id supply cap of 1), so the fee-free path cannot be reached by amount manipulation regardless of batching.

## Proof of Concept
1. Attacker controls (or colludes with) two accounts, `A` and `B`, each holding balances of a fungible-like `Nep245`/`Imt` token `T` with supply `N > 1`.
2. Instead of submitting one `TokenDiff` intent per account with `diff = {T: -N, U: +M}` (which would incur `token_fee(T, N, fee) = fee` since `amount > 1`), the attacker submits `N` separate `TokenDiff` intents per account, each with `diff = {T: -1, U: +m}` (`m = M/N`).
3. Each of the `N` intents, when executed via `TokenDiff::execute_intent`, calls `Self::token_fee(token_id, 1, protocol_fee)`, which returns `Pips::ZERO` because `amount == 1`, per: [6](#0-5) 
4. All `N` intents sum to the same net transfer of `T` and `U` as the single large trade, but `fees_collected` accumulated across the batch is `0`, whereas the equivalent single-intent trade would have collected `fee_ceil(N) > 0`.
5. `execute_signed_intents` processes the whole batch and finalizes with zero fee revenue on the full `N`-unit trade: [7](#0-6)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-78)
```rust
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

**File:** contracts/defuse/core/src/engine/mod.rs (L75-82)
```rust
        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
```
