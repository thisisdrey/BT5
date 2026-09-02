### Title
Protocol swap fees on `TokenDiff` can be fully bypassed by settling the same economic swap via two `Transfer` intents in one `execute_intents`/`simulate_intents` call — ([File: contracts/defuse/core/src/intents/tokens.rs])

### Summary
`Defuse`'s only fee-charging primitive is `TokenDiff`, which deducts `Pips` fees on every negative delta and credits them to `fee_collector`. The `Transfer` intent moves the exact same tokens between two accounts but never computes or collects any fee. Because a `MultiPayload`/`execute_intents` call can bundle independently signed payloads from multiple accounts atomically, two counterparties can achieve an economically identical bilateral swap by each signing a `Transfer` intent instead of a `TokenDiff` intent, paying zero protocol fee.

### Finding Description
`TokenDiff::execute_intent` computes `protocol_fee = engine.state.fee()` and, for every negative delta (token_in), charges `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, crediting the result to `engine.state.fee_collector()`: [1](#0-0) [2](#0-1) 

`Transfer::execute_intent`, in contrast, only performs `internal_sub_balance`/`internal_add_balance` between `sender_id` and `receiver_id`; it never references `engine.state.fee()` or `fee_collector()`: [3](#0-2) 

`simulate_intents`/`execute_intents` accept a batch of independently-signed `MultiPayload`s (`MultiPayloadArgs { signed: &[...] }`), which are all applied atomically in one call — this is exactly the mechanism the protocol itself uses to settle multi-party swaps built from several signed `TokenDiff` intents (see `swap_p2p`/`swap_many` tests): [4](#0-3) 

Two counterparties, `user1` and `user2`, who want to exchange `ft1` for `ft2` can each sign a `Transfer` intent (`user1 -> user2: ft1`, `user2 -> user1: ft2`) and submit both payloads in a single `execute_intents` call, exactly as done for `TokenDiff`-based swaps in `swap_p2p`: [5](#0-4) 

The end state (each account's balances) is identical to what a fee-charging `TokenDiff` swap would have produced, except no amount is ever debited to `fee_collector`. The binding broken is: fees owed (based on the value exchanged) versus fees collected (zero), even though the net economic effect — a mutual, atomic exchange of tokens between two parties — is the same operation the protocol fees when routed through `TokenDiff`.

### Impact Explanation
This lets any pair of colluding (or even the same actor controlling both accounts) unprivileged users bypass protocol swap fees entirely for any bilateral (or multi-party, chained via multiple `Transfer`s) token exchange, as long as they can coordinate off-chain (e.g., via a solver/relayer analogous to how RFQ swaps are already coordinated for `TokenDiff`). This directly matches "fees bypassed" under the Critical impact category — the fee recipient is deprived of revenue on every swap executed this way, while the trade still nets out correctly for the end users, indistinguishable on-chain from a legitimate no-fee "gift" transfer.

### Likelihood Explanation
Likelihood is high wherever fee (`Pips`) is set above zero: any market maker / OTC counterparties who already coordinate off-chain (which is required for `TokenDiff` swaps too, since deltas must net to zero across signers) have no incentive to use the fee-charging `TokenDiff` path when the fee-free `Transfer` path produces an identical atomic settlement within the same `execute_intents`/`simulate_intents` call. No special role, deployment misconfiguration, or privileged key is required — only two (or more) unprivileged signers agreeing on amounts.

### Recommendation
Charge the protocol fee on `Transfer` intents as well, or restrict `Transfer` so that it cannot be used to settle disguised swaps at scale (e.g., detect batches where multiple `Transfer` intents in the same `MultiPayload` execution net to a token-for-token exchange, or simply apply the same `Pips` fee logic used in `TokenDiff::execute_intent` to `Transfer::execute_intent`, or fold both into unified fee accounting keyed off net value moved per account per execution rather than per-intent-type).

### Proof of Concept
1. Configure `Defuse` with `fee > Pips::ZERO` and a `fee_collector`.
2. `user1` holds `ft1`, `user2` holds `ft2`.
3. `user1` signs `Transfer { receiver_id: user2, tokens: {ft1: 100} }`.
4. `user2` signs `Transfer { receiver_id: user1, tokens: {ft2: 200} }` (agreed off-chain as an even swap).
5. Submit both signed payloads together via `execute_intents`/`simulate_intents` (`MultiPayloadArgs { signed: [payload1, payload2] }`), exactly as the `swap_p2p` test bundles two `TokenDiff` payloads: [4](#0-3) 
6. Result: `user1` ends with 200 `ft2`, `user2` ends with 100 `ft1` — the same result a `TokenDiff` swap would produce — but `fee_collector`'s balance is unchanged, whereas the equivalent `TokenDiff` swap (per `swap_p2p`) would have deducted `TokenDiff::closure_delta(...)`-computed fees from the amounts received.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-78)
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

**File:** contracts/defuse/core/src/intents/tokens.rs (L82-128)
```rust
impl ExecutableIntent for Transfer {
    fn execute_intent<S, I>(
        self,
        sender_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        if sender_id == self.receiver_id || self.tokens.is_empty() {
            return Err(DefuseError::InvalidIntent);
        }

        engine
            .inspector
            .on_event(DefuseEvent::Transfer(Cow::Borrowed(
                [MaybeIntentEvent::new_intent(
                    AccountEvent::new(sender_id, TransferEvent::from(&self)),
                    intent_hash,
                )]
                .as_slice(),
            )));

        engine
            .state
            .internal_sub_balance(sender_id, self.tokens.clone())?;
        engine
            .state
            .internal_add_balance(self.receiver_id.clone(), self.tokens.clone())?;

        if let Some(mut notification) = self.notification {
            notification.min_gas = Some(
                notification
                    .min_gas
                    .unwrap_or(MT_ON_TRANSFER_GAS_DEFAULT)
                    .max(MT_ON_TRANSFER_GAS_MIN),
            );

            engine
                .state
                .notify_on_transfer(sender_id, self.receiver_id, self.tokens, notification);
        }

        Ok(())
    }
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L26-46)
```rust
async fn swap_p2p(
    #[values(Pips::ZERO, Pips::ONE_BIP, Pips::ONE_PERCENT)] fee: Pips,
    #[with(Env::builder().fee(fee))]
    #[future(awt)]
    env: Env,
) {
    let (user1, user2, ft1, ft2) = futures::join!(
        env.create_user(),
        env.create_user(),
        env.create_token(),
        env.create_token()
    );

    let ft1_token_id = TokenId::from(Nep141TokenId::new(ft1.contract_id().clone()));
    let ft2_token_id = TokenId::from(Nep141TokenId::new(ft2.contract_id().clone()));

    env.initial_ft_storage_deposit(
        vec![user1.account_id(), user2.account_id()],
        vec![ft1.contract_id(), ft2.contract_id()],
    )
    .await;
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L223-249)
```rust
    let signed = try_join_all(accounts.iter().flat_map(move |account| {
        account.diff.iter().cloned().map(move |diff| {
            account.account.sign_defuse_payload_default(
                &env.defuse,
                [TokenDiff {
                    diff,
                    memo: None,
                    referral: None,
                }],
            )
        })
    }))
    .await
    .unwrap();

    // simulate
    env.defuse
        .simulate_intents(MultiPayloadArgs { signed: &signed })
        .await
        .unwrap()
        .into_result()
        .unwrap();

    // verify
    env.defuse_execute_intents(env.defuse.contract_id(), signed.clone())
        .await
        .unwrap();
```
