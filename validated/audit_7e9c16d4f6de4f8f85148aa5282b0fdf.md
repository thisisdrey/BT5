### Title
Fee bypass via amount-slicing exemption on `Nep245`/`Imt` `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` exempts `Nep245`/`Imt` token deltas from protocol fees whenever the traded amount is `<= 1`, intended to avoid taxing true NFT-style transfers. Because a single signed `MultiPayload` can carry an arbitrary list of `TokenDiff` intents, and each `TokenDiff` is fee-evaluated independently per-intent, a divisible NEP-245/IMT balance can be moved in many 1-unit legs, each individually falling under the fee-exempt threshold, letting the trader pay zero aggregate fee on an arbitrarily large transfer.

### Finding Description
In `TokenDiff::execute_intent`, for every `(token_id, delta)` pair in a diff, the fee is computed as: [1](#0-0) 

and the fee rate itself comes from `token_fee`: [2](#0-1) 

For `TokenIdType::Nep245` and `TokenIdType::Imt`, fee is `Pips::ZERO` whenever `amount <= 1`; only for `amount > 1` is the configured protocol fee applied. This exemption is evaluated strictly per intent, per token, using only that single intent's delta — there is no tracking of cumulative volume traded by an account/token across intents in the same message or across multiple messages.

The engine executes an arbitrary list of intents from a `DefuseIntents` payload, iterating and calling `execute_intent` on each one independently: [3](#0-2) 

So instead of submitting one `TokenDiff{ diff: {token: -N} }` (which would trigger `fee_ceil(N)` since `N > 1`), an attacker can submit `N` separate `TokenDiff` intents (either bundled inside one signed `MultiPayload`'s intent list, or as `N` separately signed payloads passed together to `execute_intents`), each moving exactly `1` unit of the same NEP-245/IMT token. Every individual intent then satisfies `amount <= 1`, so `token_fee` returns `Pips::ZERO` for every leg, and the total fee collected across the full `N`-unit transfer is `0`, regardless of the configured `fee` in `FeesConfig`.

This breaks the "fees owed vs. fees collected" invariant: the protocol's configured fee should apply proportionally to the net amount traded, but by amount-slicing, the fees owed on the aggregate transfer are never collected.

### Impact Explanation
This is a fee-bypass: an unprivileged trader routes any size of NEP-245/IMT-denominated trade through the protocol without paying the configured protocol fee, denying revenue to the fee collector that would otherwise be owed under normal trading. This falls under the listed Critical impact class "fees bypassed or over-collected." Because NEP-245/IMT tokens in this system are used to represent wrapped balances (e.g., cross-`Defuse`-contract wrapped FTs, wrapped semi-fungible balances) rather than strictly 1-of-1 NFTs, this fee-exempt threshold is reachable on genuinely divisible value, not just true unique NFTs.

### Likelihood Explanation
High likelihood: the exploit requires no privileged role, no timing dependency, and no interaction with any other account — a single signer can construct one `MultiPayload` containing `N` `TokenDiff` intents (or `N` payloads) each transferring `1` unit, and it is immediately effective and repeatable without limit (bounded only by message/gas size, which is out of scope for likelihood assessment here). Unlike the original truncation report (which needed multiple transactions to "compound" a rounding error), this variant requires only splitting a single trade's intent list.

### Recommendation
Compute and apply the fee based on the net aggregate delta per `(account, token_id)` across all intents processed within a single `execute_intents` call (or, at minimum, remove/redesign the `amount <= 1` fee exemption so it cannot be trivially satisfied by intent-splitting — e.g., only exempt tokens whose supply/type is provably non-fungible (`Nep171` already has this exemption unconditionally), rather than granting the same exemption to divisible `Nep245`/`Imt` balances based solely on the size of a single intent's delta).

### Proof of Concept
1. Attacker holds a NEP-245/IMT-wrapped balance of `100` units of some fungible-like MT token, with protocol `fee` configured to a non-zero `Pips` value.
2. Attacker signs a single `MultiPayload` whose `DefuseIntents.intents` contains 100 separate `TokenDiff` intents, each with `diff = { <mt_token_id>: -1, <counter_token_id>: <matching_positive_delta> }` (or paired against a counterparty's matching `TokenDiff` intents to satisfy the invariant in `TransferMatcher::finalize`).
3. Submit via `execute_intents` (`contracts/defuse/src/contract/intents/mod.rs`, `Intents::execute_intents`).
4. `Engine::execute_signed_intent` → `intents.execute_intent` iterates and executes all 100 `TokenDiff` intents; for every leg `token_fee` returns `Pips::ZERO` because `amount == 1`, so `fees_collected` stays empty for all 100 legs.
5. Compare against submitting the same net `-100` delta in a single `TokenDiff` intent: `token_fee` would use `amount = 100 > 1` and apply `fee.fee_ceil(100)` non-zero.
6. Net result: the aggregate `100`-unit trade completes with `0` fees collected instead of the fee that would be owed on a single, unsplit `TokenDiff` of the same net size — a concrete value loss to the fee collector, reproducible deterministically with no time delay or privileged access.

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

**File:** contracts/defuse/core/src/engine/mod.rs (L76-83)
```rust
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
    }
```
