### Title
Protocol fee bypass on IMT/NFT/MT tokens via unit-sized `TokenDiff` splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::token_fee` decides fee-exemption per individual intent call using only that call's own `|delta|`, not the aggregate notional moved for a `TokenId` within the batch or across a trade. An attacker who controls an `Imt` minter (or trades `Nep245`/`Nep171` in the same way) can split an arbitrarily large trade into many `TokenDiff` intents each with `|delta| == 1`, so every call to `token_fee` hits the `amount <= 1` branch and returns `Pips::ZERO`, letting the whole notional bypass the protocol fee.

### Finding Description
The broken binding is: **fee actually collected for a `TokenId` across a settled batch should equal `Pips::fee_ceil` applied to the total notional moved for that token** (i.e. what a single `TokenDiff` with the same aggregate `delta` would have paid). Instead, the code computes: [1](#0-0) 

fee per-intent, using only that single intent's `delta.unsigned_abs()`: [2](#0-1) 

Each `TokenDiff` intent is executed independently — `DefuseIntents::execute_intent` iterates and calls `execute_intent` per intent, and `Engine::execute_signed_intents` iterates per signed payload — with no aggregation of deltas across intents for fee purposes: [3](#0-2) [4](#0-3) 

Balance-conservation is enforced only by `TransferMatcher::finalize`, which nets deltas to zero across the whole batch and has no notion of fees; it does not re-derive or validate the fee that should have applied to the aggregate movement: [5](#0-4) 

`TokenIdType::Imt` is a self-mintable, minter-controlled namespace (`ImtTokenId { minter_id, token_id }`), so an attacker can freely create an `Imt` token that is used purely as a fungible-value surrogate (e.g. representing/paired with a real `Nep141` asset): [6](#0-5) 

Exploit flow: Attacker (as minter) mints `imt:attacker.near:X` and structures a large trade with a counterparty (or a second self-controlled account) as `N` separate `TokenDiff` intents inside one `MultiPayload` batch, each with `diff = {Imt_token: -1, Imt_token_or_other: +1}` instead of one intent with `diff = {Imt_token: -N, ...: +M}`. Every call to `Self::token_fee(imt_token_id, 1, fee)` matches the `amount <= 1` arm and returns `Pips::ZERO`, so `fees_collected` stays empty for every one of the N intents, and `internal_add_balance(fee_collector, ...)` is never invoked for that leg, while `internal_apply_deltas` still moves the full aggregate amount across the batch once `TransferMatcher::finalize` nets it. None of `MultiPayload::verify`, nonce/salt checks, or `TransferMatcher::finalize` validate or reconstruct the fee that should apply to the aggregate notional — they only ensure signatures/nonces are valid and that deltas net to zero, which they do (the exploit doesn't break balance conservation, only fee collection).

### Impact Explanation
Whichever party or token leg is routed through the `Imt`/`Nep245`/`Nep171` fee-exempt branch, with deltas structured as sequences of `|delta| == 1`, permanently avoids paying protocol fees regardless of the true aggregate value transferred, when a single equivalent `TokenDiff` of the same total delta would have paid `Pips::fee_ceil(fee, amount)`. This is a direct "protocol fees bypassed" case (explicitly listed Critical impact), reducing `fee_collector` revenue for every such structured trade. It is fully repeatable by any unprivileged account across arbitrary tokens/batches with no bound other than batch size/gas.

### Likelihood Explanation
The attacker only needs to be a minter of an `Imt` token (unprivileged, self-controlled) and either a willing counterparty or two of their own accounts to construct a matching pair of `TokenDiff` intents inside a single `MultiPayload` batch. No role, relayer key, or victim key is required — the attacker signs their own intents. The only cost is gas for a larger batch and possible storage for many intents. This is straightforwardly reproducible via `execute_intents`/`simulate_intents`.

### Recommendation
Compute `token_fee` based on the aggregate absolute delta for a given `TokenId` across the whole executed batch (or at least require a minimum granularity/floor independent of per-intent splitting), rather than per individual `TokenDiff` intent's local `|delta|`. Alternatively, remove or tighten the `amount <= 1` fee-exemption for `Nep245`/`Imt` token types, since these types can represent fungible value unlike genuine 1-of-1 NFTs (`Nep171`).

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or a `near-workspaces` sandbox test under `tests/src/tests/defuse/intents/token_diff.rs`):
1. Mint an `Imt` token controlled by attacker account `A` (`ImtTokenId::new(A, "X")`), and fund a counterparty `B` with a real `Nep141` asset of large notional (e.g. 100,000 units).
2. **Baseline**: Execute a single `TokenDiff` from `A`: `diff = {Imt: -100000, Nep141: +100000}` paired with `B`'s complementary intent `diff = {Nep141: -100000, Imt: +100000}`, with `fee = Pips::ONE_PERCENT`. Assert `fee_collector` balance for `Nep141` (and/or `Imt` since amount>1) increases by `Pips::fee_ceil(fee, 100000)` — non-zero on the `Nep141` leg, and non-zero on `Imt` leg since `amount > 1`.
3. **Exploit**: Reset state; execute the same aggregate trade split into 100,000 separate `TokenDiff` intents from `A`/`B`, each `{Imt: -1, Nep141: +1}` / `{Nep141: -1, Imt: +1}`, all inside one `execute_intents` batch. Assert `Self::token_fee(&imt_token_id, 1, fee) == Pips::ZERO` for every call, `fees_collected` stays empty on the `Imt` leg for all 100,000 intents, and final `fee_collector` `Imt` balance is `0` despite `100000` aggregate `Imt` notional moved — violating the binding `fee_collected(token) == Pips::fee_ceil(fee, aggregate_|delta|(token))`.

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

**File:** contracts/defuse/core/src/intents/mod.rs (L97-112)
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
```

**File:** contracts/defuse/core/src/engine/mod.rs (L32-83)
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

**File:** crates/primitives/token-id/src/imt.rs (L7-27)
```rust
// Intent mintable token - can be minted only by intents 'ImtMint'
#[cfg_attr(any(feature = "arbitrary", test), derive(::arbitrary::Arbitrary))]
#[cfg_attr(
    feature = "borsh",
    derive(::borsh::BorshSerialize, ::borsh::BorshDeserialize),
    cfg_attr(feature = "borsh-schema", derive(::borsh::BorshSchema))
)]
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct ImtTokenId {
    pub minter_id: AccountId,

    pub token_id: String,
}

impl ImtTokenId {
    pub fn new(minter_id: impl Into<AccountId>, token_id: impl Into<String>) -> Self {
        Self {
            minter_id: minter_id.into(),
            token_id: token_id.into(),
        }
    }
```
