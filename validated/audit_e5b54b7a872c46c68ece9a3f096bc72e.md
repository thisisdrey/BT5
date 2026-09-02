### No vulnerability found for this question.

Tracing `TokenDiff::execute_intent` confirms the binding holds: `fees_collected` is computed from negative deltas via `Self::token_fee(...).fee_ceil(amount)`, added into a local `Amounts` accumulator, and then credited exactly once via `engine.state.internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)` [1](#0-0) . The `referral` field is only ever included as part of the `TokenDiff` struct that gets wrapped `Cow::Borrowed` into the emitted `TokenDiffEvent` for logging/inspector purposes — it is never read, matched on, or used to compute any additional `internal_add_balance` call or any other state mutation [2](#0-1) [3](#0-2) .

There is no code path in this contract that pays out to `referral` on-chain — a signer setting `referral: Some(some_account)` has zero on-chain effect beyond appearing in the emitted event log, as also demonstrated in test helpers that freely set arbitrary `referral` values without any balance side effects [4](#0-3) , and in the execution/simulation test suite that always sets `referral: None` while asserting exact fee/balance outcomes with no referral-based payouts [5](#0-4) .

The scenario in the question — a resolver or off-chain backend independently trusting `TokenDiffEvent.diff.referral` to issue a *second*, separate payout — is not a vulnerability in this repository's contract code; it would be a flaw in an external, untrusted off-chain system choosing to act on an event field that the protocol itself never treats as authoritative or actionable. That falls outside the scope of "value leaving the Verifier that the signer never authorised," since the Verifier itself never moves any funds to `referral`.

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L30-38)
```rust
pub struct TokenDiff {
    #[serde_as(as = "Amounts<BTreeMap<_, DisplayFromStr>>")]
    pub diff: TokenDeltas,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memo: Option<String>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub referral: Option<AccountId>,
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L69-101)
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
        }

        engine.inspector.on_event(DefuseEvent::TokenDiff(
            [MaybeIntentEvent::new_intent(
                AccountEvent::new(
                    signer_id,
                    TokenDiffEvent {
                        diff: Cow::Borrowed(&self),
                        fees_collected: fees_collected.clone(),
                    },
                ),
                intent_hash,
            )]
            .as_slice()
            .into(),
        ));

        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
        }
```

**File:** contracts/defuse/core/src/events/tests/mod.rs (L127-148)
```rust
fn token_diff_intent_event<'a>() -> DefuseEvent<'a> {
    DefuseEvent::TokenDiff(Cow::Owned(vec![MaybeIntentEvent::new_intent(
        AccountEvent {
            account_id: account(),
            event: TokenDiffEvent {
                fees_collected: tokens(),
                diff: Cow::Owned(TokenDiff {
                    diff: Amounts::new(
                        [
                            (TokenId::Nep141("token.near".parse().unwrap()), 100),
                            (TokenId::Nep245("token.near:abcd".parse().unwrap()), -200),
                        ]
                        .into(),
                    ),
                    memo: Some("test token diff".to_string()),
                    referral: Some(account().into()),
                }),
            },
        },
        [0; 32],
    )]))
}
```

**File:** tests/src/tests/defuse/intents/token_diff.rs (L223-234)
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
```
