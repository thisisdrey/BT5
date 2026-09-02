### Title
Protocol fee bypass via IMT sub-token splitting - (File: `contracts/defuse/core/src/intents/token_diff.rs:211`)

### Summary
`TokenDiff::token_fee` waives protocol fees whenever a `TokenIdType::Imt` (or `Nep245`) leg has `amount <= 1`, an exemption designed for genuinely non-fungible transfers. Because IMT sub-token ids (`imt:<minter_id>:<token_id>`) are freely chosen strings fully controlled by the minter, an attacker can mint N sub-tokens of amount 1 each and swap them in a single `TokenDiff` batch, collecting `Pips::ZERO` fee on every leg instead of the `Pips::fee_ceil(fee, N)` that would be owed if the same economic value were expressed as one leg with `amount = N`.

### Finding Description
The broken binding: for an equivalent economic transfer of N units of IMT value, `fee_owed(single leg, amount=N) = Pips::fee_ceil(fee, N)` should equal `fee_owed(N legs, amount=1 each) = Σ Pips::fee_ceil(fee, 1)`. In `TokenDiff::token_fee` at [1](#0-0) , the match arm `TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}` only charges fee when a *single* `TokenId`'s delta magnitude exceeds 1; otherwise it returns `Pips::ZERO`. `execute_intent` computes and accumulates this per `(token_id, delta)` pair independently in the loop at [2](#0-1) , so fee assessment is entirely local to each distinct `TokenId` string, never to the aggregate value moved.

The root cause is that `ImtTokenId`'s `token_id` field is an attacker-supplied arbitrary string, bound only to the minter's `AccountId`, as seen in [3](#0-2) . Via the `ImtMint` intent at [4](#0-3) , the attacker (as their own minter) can freely allocate N distinct sub-token ids for what is functionally one fungible balance, then present a `TokenDiff` with N separate `TokenId` entries each carrying `delta = -1`/`+1`, each independently qualifying for the `amount <= 1` fee exemption. No existing guard (`internal_apply_deltas`, `Amounts::add`, or the fee-collection step) aggregates fee assessment across sibling `TokenId`s sharing the same minter, so the divergence is real and reachable purely by an unprivileged attacker minting/burning/swapping their own IMT tokens.

### Impact Explanation
This lets an attacker completely bypass the protocol's `Pips` fee on any amount of value routed through IMT tokens they mint, at zero fee versus the `fee_ceil(fee, N)` due on an equivalent single-leg swap. This matches the "protocol fees bypassed" Critical category. The blast radius is limited to trades involving attacker-minted IMT tokens (the attacker must be the minter or the counterparty must accept these sub-tokens as having real value), but the mechanism is fully repeatable across accounts and batches with no cost beyond gas.

### Likelihood Explanation
Preconditions are trivial for an unprivileged attacker: mint N IMT sub-tokens of amount 1 via `ImtMint` (self-minter authority requires no special role, per [5](#0-4) ), then submit a single signed `TokenDiff` batch swapping all N legs. Cost is only gas and normal minting mechanics; the exploit is fully repeatable and requires no counterpart privilege.

### Recommendation
Aggregate fee assessment for `Nep245`/`Imt` token types by minter/contract authority (or by total absolute delta across all sub-token ids sharing the same minter within a single `TokenDiff`) rather than per individual `TokenId` string, so that splitting one fungible-value transfer into many `amount=1` legs cannot evade the `amount > 1` threshold check.

### Proof of Concept
```rust
// cargo test (near-workspaces sandbox or unit-level engine test)
// 1. As attacker, call ImtMint minting N=1000 sub-tokens
//    "imt:<attacker>:t0" .. "imt:<attacker>:t999", each amount = 1, to self.
// 2. Submit one TokenDiff intent containing 1000 (token_id, delta=-1) legs
//    swapping them all against some counter-token, in a single execute_intents batch.
// 3. Assert fees_collected total == 0 (from TokenDiffEvent::fees_collected as emitted
//    at token_diff.rs execute_intent).
// 4. Construct an equivalent TokenDiff with a single ImtTokenId leg, amount = 1000,
//    same protocol_fee. Assert fee = TokenDiff::token_fee(imt_token_id, 1000, fee).fee_ceil(1000) > 0.
// 5. Assert step 3's collected fee (0) != step 4's expected fee (>0), proving the
//    fee_owed(single leg, N) == fee_owed(N legs, 1 each) binding is violated.
```

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

**File:** crates/primitives/token-id/src/imt.rs (L15-28)
```rust
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
}
```

**File:** contracts/defuse/core/src/intents/imt.rs (L19-40)
```rust
pub struct ImtMint {
    /// Receiver of the minted tokens
    pub receiver_id: AccountId,

    /// The `token_ids` will be wrapped to bind the token ID to the
    /// minter authority (i.e. signer of this intent).
    /// The final string representation of the token will be as follows:
    /// `imt:<minter_id>:<token_id>`
    #[serde_as(as = "Amounts<BTreeMap<_, DisplayFromStr>>")]
    pub tokens: ImtTokens,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memo: Option<String>,

    /// Optionally notify `receiver_id` via `mt_on_transfer()`
    ///
    /// NOTE: `min_gas` is adjusted with following values:
    /// * minimum: 5TGas
    /// * default: 30TGas
    #[serde(flatten, default, skip_serializing_if = "Option::is_none")]
    pub notification: Option<NotifyOnTransfer>,
}
```

**File:** contracts/defuse/core/src/intents/imt.rs (L42-71)
```rust
impl ExecutableIntent for ImtMint {
    #[inline]
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
        engine
            .inspector
            .on_event(DefuseEvent::ImtMint(Cow::Borrowed(
                [MaybeIntentEvent::new_intent(
                    AccountEvent::new(signer_id, ImtMintEvent::from(&self)),
                    intent_hash,
                )]
                .as_slice(),
            )));

        engine.state.imt_mint(
            signer_id,
            self.receiver_id,
            self.tokens,
            self.memo,
            self.notification,
        )
    }
```
