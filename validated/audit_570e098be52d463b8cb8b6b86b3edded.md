### Title
Fee bypass on NEP-245/IMT `TokenDiff` legs by splitting a large fungible amount into many unit-magnitude intents in one signed payload - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives fees on `TokenIdType::Nep245`/`TokenIdType::Imt` whenever the per-intent `amount <= 1`, a rule intended to exempt true NFT-style singleton transfers. Because a single signed `DefusePayload` can bundle an arbitrary array of `Intent::TokenDiff` entries (`DefuseIntents { intents: Vec<Intent> }`), each executed independently with its own fresh fee computation, an attacker can split one large fungible MT/IMT amount into many `delta = -1` legs and pay zero aggregate fee instead of `fee_ceil(total_amount)`.

### Finding Description
Broken binding: `fee_collector` balance increase for token `T` across a batch should equal `Pips::fee_ceil(protocol_fee, Σ|negative deltas of T|)`, but for `TokenIdType::Nep245`/`Imt` split into unit legs it equals `0`.

Code path:
- `TokenDiff::execute_intent` (contracts/defuse/core/src/intents/token_diff.rs:41-104) loops over `self.diff` — the map belonging to *one* `TokenDiff` intent instance — and for each negative delta computes `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` freshly, with `fees_collected` reset per intent (line 57). [1](#0-0) 
- `Self::token_fee` (contracts/defuse/core/src/intents/token_diff.rs:206-216): for `TokenIdType::Nep245 | TokenIdType::Imt`, if `amount > 1` the configured `fee` is applied; otherwise (`amount <= 1`) it returns `Pips::ZERO`. [2](#0-1) 
- A single signed message carries `message: DefuseIntents { intents: Vec<Intent> }` under one `nonce`/signature, and `Intent::execute_intent` dispatches each array element to its own `TokenDiff::execute_intent` call independently. [3](#0-2) 
- Balance debits, however, aggregate correctly regardless of how the delta is split: `Deltas::internal_sub_balance` forwards each leg to `TransferMatcher::withdraw`, which accumulates per-owner, per-token withdrawal totals via `TokenTransferMatcher::sub_add` (additive accounting), so 500×(-1) legs produce the exact same net balance and `Transfers` output as one -500 leg. [4](#0-3) [5](#0-4) 

Exploit: attacker holds ≥500 units of a fungible NEP-245 (or IMT) `token_id`. Instead of signing one `TokenDiff{diff:{T: -500, U: +X}}`, the attacker (or attacker+counterparty) signs a payload whose `intents` array contains 500 separate `TokenDiff` items, each `diff = {T: -1}` (with the matching +delta legs, from itself or a counterparty, split similarly or in one leg — `TransferMatcher::finalize` only requires the *aggregate* per-token deltas across the whole batch to net to zero, not that each intent be balanced). Each of the 500 `TokenDiff::execute_intent` calls independently evaluates `token_fee(T, amount=1, fee)` → `Pips::ZERO`, so `fees_collected` is empty every time and `internal_add_balance(fee_collector, ...)` is never invoked for token `T`. The signer's balance is still debited a net 500 `T`, identical to the non-split path, but `fee_collector` receives nothing instead of `fee.fee_ceil(500)`.

No existing guard prevents this: nonce/signature checks (`verify_intent_nonce`, `commit_nonce`) only govern replay of the whole payload, not the internal intent count or per-intent amount; `TransferMatcher::finalize` only enforces net-zero deltas across the whole batch, not fee correctness; there is no minimum-amount or intent-count restriction, and no cross-intent aggregation of `amount` before calling `token_fee`.

### Impact Explanation
Protocol fee revenue on NEP-245/IMT `TokenDiff` swaps is fully bypassable for any fungible-quantity multi-token, at attacker's discretion, for any batch size and any fee configuration — this is exactly the "protocol fees bypassed" Critical category. The loss falls on `fee_collector` (protocol revenue), is repeatable indefinitely across accounts/tokens/batches, and scales with the value being swapped (attacker can always structure a swap of any size as N unit legs). It requires no privileged role — any unprivileged signer with sufficient balance of a NEP-245/IMT token can perform it in a single transaction bundling many `TokenDiff` intents (limited only by gas/storage, which is out of scope but does not prevent the vulnerability's existence at meaningful scale).

### Likelihood Explanation
Preconditions are minimal and fully attacker-controlled: own signer account, an existing NEP-245/IMT balance in Defuse ≥ the swap amount, and a non-zero `FeesConfig::fee`. The attacker only needs to construct a `DefuseIntents` array with many `TokenDiff` entries instead of one, sign it once, and call `execute_intents`/`simulate_intents`. Gas costs scale linearly with the number of legs but no other cost or barrier exists; this is trivially and repeatably exploitable by any user who wants to avoid paying fees on MT/IMT swaps.

### Recommendation
Compute the NEP-245/IMT fee-exemption threshold on the *aggregate* negative delta per `token_id` across the whole `DefuseIntents`/batch rather than per individual `TokenDiff` intent (e.g., accumulate per-token negative deltas across all `TokenDiff` intents before calling `token_fee`, or move the `amount <= 1` exemption check to operate on the summed magnitude actually withdrawn for that token in the finalized `TransferMatcher`, not on the delta of an isolated intent). Alternatively, remove/limit the NFT-style fee exemption to token types that are provably non-fungible (e.g., only for `TokenIdType::Nep171`, since NEP-245/IMT can represent fungible sub-token quantities), or reject `TokenDiff` intents whose `|delta| == 1` unless there is a single occurrence of that `token_id` across the entire signed batch.

### Proof of Concept
```rust
// pseudocode based on tests/src/tests/defuse/intents/token_diff.rs patterns,
// using near-workspaces sandbox (Env/DefuseSignerExt helpers already in repo)

#[tokio::test]
async fn fee_bypass_via_unit_legs() {
    let fee = Pips::ONE_PERCENT; // non-zero fee
    let env = Env::builder().fee(fee).build().await;
    let attacker = env.create_user().await;
    let mt_contract = env.create_mt_token().await; // NEP-245
    let token_id = TokenId::from(Nep245TokenId::new(mt_contract.contract_id().clone(), "id1".into()));

    env.defuse_mt_deposit_to(mt_contract.contract_id(), "id1", 500, attacker.account_id()).await.unwrap();

    // Case A: single TokenDiff intent with delta = -500 (paired with a counter-leg to net to zero)
    // fee_ceil(500) should be collected
    let signed_a = attacker.sign_defuse_payload_default(&env.defuse, [
        TokenDiff { diff: TokenDeltas::new([(token_id.clone(), -500)].into()), memo: None, referral: None },
        // + matching counterparty leg to satisfy TransferMatcher::finalize
    ]).await.unwrap();
    env.defuse_execute_intents(env.defuse.contract_id(), [signed_a]).await.unwrap();
    let fee_collector_balance_a = env.mt_balance_of(env.defuse.fee_collector(), &token_id).await;
    assert!(fee_collector_balance_a > 0); // expected: fee.fee_ceil(500)

    // Case B: 500 separate TokenDiff intents, each delta = -1, bundled in ONE signed payload
    let legs: Vec<TokenDiff> = (0..500).map(|_| TokenDiff {
        diff: TokenDeltas::new([(token_id.clone(), -1)].into()),
        memo: None, referral: None,
    }).collect();
    let signed_b = attacker.sign_defuse_payload_default(&env.defuse, legs).await.unwrap();
    env.defuse_execute_intents(env.defuse.contract_id(), [signed_b]).await.unwrap();
    let fee_collector_balance_b = env.mt_balance_of(env.defuse.fee_collector(), &token_id).await;

    // BROKEN BINDING: fee collected for equal total amount (500) should match, but doesn't
    assert_eq!(fee_collector_balance_a, fee_collector_balance_b); // FAILS: b == 0, a > 0
}
```

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

**File:** contracts/defuse/core/src/intents/mod.rs (L115-145)
```rust
impl ExecutableIntent for Intent {
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
        match self {
            Self::AddPublicKey(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::RemovePublicKey(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::Transfer(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::FtWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::NftWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::MtWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::NativeWithdraw(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::StorageDeposit(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::TokenDiff(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            Self::SetAuthByPredecessorId(intent) => {
                intent.execute_intent(signer_id, engine, intent_hash)
            }
            Self::AuthCall(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            #[cfg(feature = "imt")]
            Self::ImtMint(intent) => intent.execute_intent(signer_id, engine, intent_hash),
            #[cfg(feature = "imt")]
            Self::ImtBurn(intent) => intent.execute_intent(signer_id, engine, intent_hash),
        }
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L151-164)
```rust
    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_sub_balance(owner_id, [(token_id.clone(), amount)])?;
            if !self.deltas.withdraw(owner_id.to_owned(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L295-333)
```rust
impl TokenTransferMatcher {
    #[inline]
    pub fn deposit(&mut self, owner_id: AccountId, amount: u128) -> bool {
        Self::sub_add(&mut self.withdrawals, &mut self.deposits, owner_id, amount)
    }

    #[inline]
    pub fn withdraw(&mut self, owner_id: AccountId, amount: u128) -> bool {
        Self::sub_add(&mut self.deposits, &mut self.withdrawals, owner_id, amount)
    }

    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, delta: i128) -> bool {
        let amount = delta.unsigned_abs();
        if delta.is_negative() {
            self.withdraw(owner_id, amount)
        } else {
            self.deposit(owner_id, amount)
        }
    }

    fn sub_add(
        sub: &mut AccountAmounts,
        add: &mut AccountAmounts,
        owner_id: AccountId,
        mut amount: u128,
    ) -> bool {
        let s = sub.amount_for(&owner_id);
        if s > 0 {
            let a = s.min(amount);
            sub.sub(owner_id.clone(), a)
                .unwrap_or_else(|| unreachable!());
            amount = amount.saturating_sub(a);
            if amount == 0 {
                return true;
            }
        }
        add.add(owner_id, amount).is_some()
    }
```
