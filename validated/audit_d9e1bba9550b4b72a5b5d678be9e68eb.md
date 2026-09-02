### Title
Protocol fees bypassed on Nep245/Imt multi-unit tokens by chunking a single large `TokenDiff` into N unit `TokenDiff` intents in one signed batch - (`contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` waives the fee on `Nep245`/`Imt` token diffs whenever the per-intent `amount <= 1`, and `TokenDiff::execute_intent` computes this fee independently for each `TokenDiff` intent in the batch rather than for the aggregate economic transfer. Since a single signed `MultiPayload` may contain an arbitrary `Vec<Intent>` of `TokenDiff`s, an attacker can split one intended transfer of `N` units of a `Nep245`/`Imt` token into `N` separate `TokenDiff` intents, each with `delta = -1`, driving the fee to zero on every one of them instead of the `fee_ceil(N)` that a single `TokenDiff` with `delta = -N` would incur.

### Finding Description
Binding claimed broken: `total_fee_collected(batch moving N units of token_id) == Pips::fee_ceil(N, fee)` regardless of how the `N` units are partitioned across `TokenDiff` intents inside the same signed `MultiPayload`.

Code path:
- `TokenDiff::execute_intent` iterates each `(token_id, delta)` pair inside a single `TokenDiff.diff` and computes the fee per-pair via `Self::token_fee(token_id, delta.unsigned_abs(), protocol_fee).fee_ceil(amount)` — see `contracts/defuse/core/src/intents/token_diff.rs:70-78`.
- `TokenDiff::token_fee` explicitly special-cases `Nep245`/`Imt`: `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` falls through to charging `fee`, but `TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO` when `amount <= 1` — `contracts/defuse/core/src/intents/token_diff.rs:206-216`.
- A `DefuseIntents` message (the payload signed once, under one nonce) is `pub intents: Vec<Intent>` with no upper bound and explicitly designed to hold many intents executed independently — `contracts/defuse/core/src/intents/mod.rs:28-113`. Each `Intent::TokenDiff(...)` is dispatched to `TokenDiff::execute_intent` independently, so the fee waiver in `token_fee` is evaluated per-intent, not on the sum of deltas the signer effects on that `token_id` across the whole `MultiPayload`.
- `Engine::execute_signed_intent` verifies signature and commits `nonce` once per `MultiPayload`, and then simply calls `intents.execute_intent(...)` over the full `Vec<Intent>` — `contracts/defuse/core/src/engine/mod.rs:42-83`. There is no aggregation of deltas per `token_id` across intents before fee calculation; aggregation into `Transfers` via `TransferMatcher::finalize` happens only for balancing withdrawals/deposits, not for fee computation, which is already computed and credited to `fee_collector` at intent-execution time (`internal_add_balance` at `contracts/defuse/core/src/intents/token_diff.rs:96-101`).

Exploit: a signer holding `N` units of a `Nep245` token, wanting to convert/withdraw all `N` units, signs one `MultiPayload` containing `N` separate `TokenDiff` intents each `{token_id: -1, matched_out_token: +k}` instead of one `TokenDiff` with `{token_id: -N, matched_out_token: +N*k}`. Each of the `N` intents independently hits `token_fee`'s `amount <= 1` branch and returns `Pips::ZERO`, so `fee_ceil(1) = 0` per intent, and the total fee credited to `fee_collector` is `0` instead of `fee_ceil(N)`.

No existing guard prevents this: `MultiPayload::verify`/nonce commitment operates once per signed message regardless of how many intents it carries (`contracts/defuse/core/src/engine/mod.rs:75-77`), and `TransferMatcher::finalize` only checks that net deltas are zero-sum (`contracts/defuse/core/src/engine/state/deltas.rs:265-283`), it does not re-derive or correct fees.

### Impact Explanation
Protocol fee revenue on any `Nep245`/`Imt` multi-unit asset can be driven to zero by any unprivileged signer simply by restructuring their own signed intent batch, at the cost of a larger payload/more intents in the same transaction. This is a direct, repeatable loss of protocol fee revenue (matches the Critical category "protocol fees bypassed or over-collected") for every Nep245/Imt token that has `fee > 0` configured, for any signer, any number of times.

### Likelihood Explanation
Preconditions are trivial and fully within an unprivileged attacker's control: hold `N` units of some `Nep245`/`Imt` token, have a fee-collector configured with `fee > 0` (already an admin-configured, non-attacker-controlled precondition, but a realistic standard deployment state), and sign a single `MultiPayload` with `N` `TokenDiff` intents instead of one. The only cost is the marginal gas/tx-size overhead of listing more intents in one call, which is well within achievable limits for realistic token amounts (e.g., splitting into tens or hundreds of chunks materially reduces fees without requiring N to be huge — the attacker can chunk to whatever count fits gas/size limits and still capture proportional fee savings, or fully bypass fees if the whole desired amount is small enough to fit as multiple 1-unit legs). This is fully repeatable across accounts, tokens, and batches.

### Recommendation
Compute the fee waiver based on the aggregate net delta per `(signer_id, token_id)` across the entire `DefuseIntents`/`MultiPayload`, not per individual `TokenDiff` intent — e.g., pre-sum all `TokenDiff` deltas per token per signer before invoking `Self::token_fee`, or move fee assessment to `TransferMatcher::finalize` where the true aggregate withdrawal amount per token is known. Alternatively, disallow the `amount <= 1` fee waiver for `Nep245`/`Imt` when there exists more than one `TokenDiff` intent from the same signer touching the same `token_id` within the same `DefuseIntents` message.

### Proof of Concept
`cargo test` (unit or `near-workspaces` sandbox) plan:
1. Configure `fee_collector` with `fee = Pips::ONE_PERCENT` (or any `fee > 0`).
2. Deposit a `Nep245` token `token_id` with `N` units (e.g., `N = 10`) to a signer account, plus enough of a counter token to balance the diffs.
3. Case (a): sign one `MultiPayload` with a single `TokenDiff { diff: {token_id: -10, other_token: +X} }` (and a matching counter-intent from a second signer to satisfy `TransferMatcher::finalize`). Execute via `execute_intents`. Assert `fee_collector`'s balance of `token_id` increases by `Pips::ONE_PERCENT.fee_ceil(10)` (non-zero).
4. Case (b): sign one `MultiPayload` with 10 separate `TokenDiff` intents, each `{diff: {token_id: -1, other_token: +X/10}}` (plus matching counter-intents). Execute via `execute_intents`. Assert `fee_collector`'s balance of `token_id` increases by `0`.
5. Assert the fee collected in (a) != fee collected in (b) despite both moving the same net `10` units of `token_id` for the same signer, demonstrating the fee bypass. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** contracts/defuse/core/src/intents/mod.rs (L28-113)
```rust
#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
}

#[cfg_attr(feature = "schemars-v0_8", derive(::schemars::JsonSchema))]
#[derive(Debug, Clone, Serialize, Deserialize, From)]
#[serde(tag = "intent", rename_all = "snake_case")]
pub enum Intent {
    /// See [`AddPublicKey`]
    AddPublicKey(AddPublicKey),

    /// See [`RemovePublicKey`]
    RemovePublicKey(RemovePublicKey),

    /// See [`Transfer`]
    Transfer(Transfer),

    /// See [`FtWithdraw`]
    FtWithdraw(FtWithdraw),

    /// See [`NftWithdraw`]
    NftWithdraw(NftWithdraw),

    /// See [`MtWithdraw`]
    MtWithdraw(MtWithdraw),

    /// See [`NativeWithdraw`]
    NativeWithdraw(NativeWithdraw),

    /// See [`StorageDeposit`]
    StorageDeposit(StorageDeposit),

    /// See [`TokenDiff`]
    TokenDiff(TokenDiff),

    /// See [`SetAuthByPredecessorId`]
    SetAuthByPredecessorId(SetAuthByPredecessorId),

    /// See [`AuthCall`]
    AuthCall(AuthCall),

    // See [`ImtMint`]
    #[cfg(feature = "imt")]
    ImtMint(ImtMint),

    // See [`ImtBurn`]
    #[cfg(feature = "imt")]
    ImtBurn(ImtBurn),
}

pub trait ExecutableIntent {
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector;
}

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

**File:** contracts/defuse/core/src/engine/mod.rs (L42-83)
```rust
    fn execute_signed_intent(&mut self, signed: MultiPayload) -> Result<()> {
        // verify signed payload and get public key
        let public_key = signed.verify().ok_or(DefuseError::InvalidSignature)?;

        // calculate intent hash
        let hash = signed.hash();

        // extract NEP-413 payload
        let DefusePayload::<DefuseIntents> {
            signer_id,
            verifying_contract,
            deadline,
            nonce,
            message: intents,
        } = signed.extract_defuse_payload()?;

        // check recipient
        if verifying_contract != *self.state.verifying_contract() {
            return Err(DefuseError::WrongVerifyingContract);
        }

        self.inspector.on_deadline(deadline);

        // make sure message is still valid
        if deadline < Timestamp::now() {
            return Err(DefuseError::DeadlineExpired);
        }

        // make sure the account has this public key
        if !self.state.has_public_key(&signer_id, &public_key) {
            return Err(DefuseError::PublicKeyNotExist(signer_id, public_key));
        }

        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
    }
```

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L265-283)
```rust
    // Finalizes all transfers, or returns unmatched deltas.
    // If unmatched deltas overflow, then Err(None) is returned.
    pub fn finalize(self) -> Result<Transfers, InvariantViolated> {
        let mut transfers = Transfers::default();
        let mut deltas = TokenDeltas::default();
        for (token_id, transfer_matcher) in self.0 {
            if let Err(unmatched) = transfer_matcher.finalize_into(&token_id, &mut transfers)
                && (unmatched == 0 || deltas.apply_delta(token_id, unmatched).is_none())
            {
                return Err(InvariantViolated::Overflow);
            }
        }
        if !deltas.is_empty() {
            return Err(InvariantViolated::UnmatchedDeltas {
                unmatched_deltas: deltas,
            });
        }
        Ok(transfers)
    }
```
