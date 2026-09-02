### Title
Protocol fee on `Nep245`/`Imt` token diffs can be bypassed by splitting a large transfer into unit-sized `TokenDiff` intents - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee()` waives the protocol fee whenever the *per-intent* delta magnitude is `<= 1` for `Nep245`/`Imt` token types (the exemption meant for one-off NFT transfers). Because this threshold is evaluated independently for each `TokenDiff` intent rather than on the cumulative amount a signer moves, a user can convert one large fee-liable swap into many unit-sized `TokenDiff` intents inside a single signed `DefuseIntents` payload and pay zero fee on the whole amount — the exact "structuring" bug class described in the GMX virtual-impact report, applied to fee thresholds instead of price-impact thresholds.

### Finding Description
`TokenDiff::execute_intent` computes the fee for each token in the diff based solely on that single intent's delta: [1](#0-0) 

The fee itself is determined by `token_fee`, which checks the magnitude of that one delta against a hardcoded threshold of `1`: [2](#0-1) 

`TokenIdType::Nep245` and `TokenIdType::Imt` are not pure 1-of-1 NFTs — both are used to represent effectively fungible balances with large quantities, as shown by `ImtMint` minting quantities like `1000` and `Nep245` wrapping of fungible-token balances with quantities like `600`: [3](#0-2) [4](#0-3) 

A `DefuseIntents` payload can contain an arbitrary list of `Intent`s, all executed sequentially under one signature/nonce: [5](#0-4) 

So instead of submitting one `TokenDiff` with `delta = -N` on a `Nep245`/`Imt` token (which would trigger `token_fee(amount=N>1, fee)` = the configured protocol fee), a signer submits `N` separate `TokenDiff` intents, each with `delta = -1` on the same token. Every one of these intents independently satisfies `amount <= 1`, so `token_fee` returns `Pips::ZERO` for each, and `fees_collected` stays empty for the entire `N`-unit transfer. The counter-party side of the trade (positive deltas) is unaffected since fees are only taken from negative deltas, so the trade still nets/matches via the settlement invariant enforced in `TransferMatcher::finalize`, just with the protocol fee reduced to zero.

**Binding broken:** fees owed (computed as if this were a single `-N` diff) versus fees actually collected (`Pips::ZERO` for every unit-sized diff) — i.e. `Σ token_fee(unit deltas) < token_fee(Σ deltas)`.

### Impact Explanation
This is a direct fee bypass: the protocol fee, which is meant to apply proportionally to `Nep245`/`Imt` token diffs above 1 unit, can be reduced to zero for any size of transfer by chopping it into unit-sized intents within the same signed payload/transaction, at the cost only of a larger message. This matches the Critical impact category "fees bypassed or over-collected."

### Likelihood Explanation
No privileged role, relayer, or victim key is required — a single unprivileged signer (or a pair of counterparties cooperating to keep the swap balanced) can construct this payload themselves. The only cost is constructing `N` `TokenDiff` entries in one `DefuseIntents` list, which is a normal, permitted operation. The larger `N` (i.e., the larger the fee-liable amount), the more entries are needed, but there is no protocol-level cap preventing this restructuring.

### Recommendation
Apply the fee-exemption threshold check on the aggregate magnitude a signer moves per token per execution (or per settlement), not on each individual `TokenDiff` intent's delta. For example, accumulate per-token, per-signer negative deltas across all intents in the execution before evaluating `amount <= 1`, or restrict the “no-fee” exemption strictly to true `Nep171` NFTs and require `Nep245`/`Imt` diffs to always incur the protocol fee regardless of per-intent amount.

### Proof of Concept
1. Configure `intents.near` with a non-zero `fee` (`Pips`) and let a user hold `N` units of a `Nep245`/`Imt` token (e.g., a wrapped fungible balance, quantities like the `600`-unit `Nep245` balance shown in `mt_transfer_call_circullar_callback`).
2. Baseline: sign one `DefuseIntents` payload with a single `TokenDiff { diff: { token: -N, other_token: +M } }`. `token_fee(N, fee)` = configured `fee` (since `N > 1`), so `fees_collected` is non-empty and the protocol fee collector receives `fee_ceil(N)`.
3. Bypass: sign one `DefuseIntents` payload containing `N` separate `TokenDiff` intents, each `{ diff: { token: -1, other_token: +m_i } }`, with a counterparty (solver) supplying matching positive/negative legs so the batch nets to zero per `TransferMatcher::finalize`.
4. In step 3, for every one of the `N` intents, `TokenDiff::execute_intent` computes `amount = 1`, so `token_fee` returns `Pips::ZERO` (`contracts/defuse/core/src/intents/token_diff.rs:211-213`), and `fees_collected` remains empty for all `N` intents — the fee collector receives nothing despite the same total `N` units moving, versus the non-zero fee collected in step 2 for an identical net transfer. [2](#0-1)

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

**File:** tests/src/tests/defuse/intents/imt_mint.rs (L40-49)
```rust
    let token = "sometoken.near".to_string();
    let memo = "Some memo";
    let amount = 1000;

    let intent = ImtMint {
        tokens: Amounts::new(std::iter::once((token.clone(), amount)).collect()),
        memo: Some(memo.to_string()),
        receiver_id: user.account_id().clone(),
        notification: None,
    };
```

**File:** tests/src/tests/defuse/tokens/nep245/mod.rs (L1455-1465)
```rust
    let refund_amount = user
        .mt_transfer_call(
            env.defuse.contract_id(),
            defuse2.account_id(),
            &ft_id.to_string(),
            600,
            None,
            serde_json::to_string(&deposit_message).unwrap(),
        )
        .await
        .expect("mt_transfer_call should succeed");
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
