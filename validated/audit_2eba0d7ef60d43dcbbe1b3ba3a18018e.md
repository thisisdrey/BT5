## Title
Protocol fee bypass on `Imt`/`Nep245` volume by splitting a `TokenDiff` into unit-sized (`|delta| <= 1`) sub-diffs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

## Summary
`TokenDiff::token_fee` (`contracts/defuse/core/src/intents/token_diff.rs:206-217`) exempts `TokenIdType::Imt` (and `Nep245`) legs from the protocol fee whenever the per-entry `amount <= 1`, and `TokenDiff::execute_intent` (lines 41-104) computes and collects fees per individual `(token_id, delta)` entry within each `TokenDiff` intent, not on the net volume traded across a batch or session. Since a single `DefusePayload`/`MultiPayload` (one signature, one nonce) can carry an arbitrary number of `TokenDiff` sub-intents, an attacker can fragment one `-1000` `Imt`-token leg into 1000 `-1` legs, each individually amount-exempted, collecting `0` fee in aggregate instead of `fee * 1000`.

## Finding Description
The intended invariant is: `fee_collected(aggregate Imt volume V traded) == protocol_fee.fee_ceil(V)` regardless of how the trade is expressed in signed intents. The code breaks this because the fee decision is local to each `TokenDiff` entry's `amount = |delta|`:

```
contracts/defuse/core/src/intents/token_diff.rs:206-217
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

and in `execute_intent`:
```
contracts/defuse/core/src/intents/token_diff.rs:59-78
for (token_id, delta) in &self.diff {
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        fees_collected.add(token_id.clone(), fee)...
    }
}
```

This `amount <= 1` exemption is a legitimate carve-out for `Nep171` (NFTs, which by definition only ever move in units of 1) and for genuinely atomic single-unit `Nep245`/`Imt` transfers, but it was extended to `Imt`/`Nep245` types whose amounts are otherwise arbitrary (fungible-like semantics inside a multi-token contract, per `crates/primitives/token-id/src/imt.rs`). Nothing constrains a signer to express a trade as one `TokenDiff` with `delta = -1000`; the same net balance effect can be expressed as N `TokenDiff` intents each with `delta = -1`, all inside a single signed `DefuseIntents` payload (one signature, one nonce, verified once by `execute_signed_intent` in `contracts/defuse/core/src/engine/mod.rs:42-83`). The engine applies each `TokenDiff` intent independently (`intents.execute_intent(&signer_id, self, hash)?`), so fee computation happens per-entry with no memory of prior entries in the same payload or batch.

The batch-level invariant enforced by `TransferMatcher::finalize` (`contracts/defuse/core/src/engine/state/deltas.rs`) only checks that the sum of raw balance deltas across the whole batch nets to zero (no free minting/burning) — it has no knowledge of, and does not correct for, how much fee should have been collected. It only guards against unmatched deltas, not fee under-collection. `MultiPayload::verify`, nonce checks, and `assert_one_yocto` are all about signature/replay integrity, not fee amount, so none of them prevent this fragmentation.

Exploit flow: attacker (as either side of a two-party Imt-token swap, or with a self-matching counter-intent) signs a single `DefusePayload` containing 1000 `TokenDiff` intents, each `{diff: {imt_token: -1, other_token: +k}}`, matched by a counter-party's (or their own alt-key's) intents providing the offsetting `+1`/`-k` legs. Every one of the 1000 sub-diffs has `amount = 1`, so `token_fee` returns `Pips::ZERO` for every leg, and `fees_collected` for the whole trade is `0`, versus `protocol_fee.fee_ceil(1000)` if expressed as one `TokenDiff{diff:{imt_token:-1000}}`.

## Impact Explanation
This under-collects protocol fees on `Imt` (and `Nep245`) token volume traded through `TokenDiff` intents, denying revenue to `fee_collector` that the protocol fee schedule (`FeesConfig`, `contracts/defuse/core/src/fees.rs`) is supposed to guarantee on every such trade. No user funds are stolen and no balance-invariant is broken (the `TransferMatcher` still nets to zero), but this is a "protocol fees bypassed" scenario, which the prompt's rules classify as Critical impact. It is fully repeatable: any account/pair of accounts trading any volume of any `Imt` or `Nep245` token can apply this fragmentation on every trade, at the cost of larger payload size (more line items, same single signature/nonce), with no privileged role required.

## Likelihood Explanation
Preconditions are minimal: the attacker only needs to be an ordinary Defuse user with `Imt` (or `Nep245`) balances and a counterparty (or a second controlled account) willing to sign the matching legs — the same requirement as any normal `TokenDiff` swap. No special role, relayer key, or upgrade capability is needed. The additional cost is purely the larger payload/gas footprint of listing N unit legs instead of one aggregate leg, which is a routine engineering trade rather than a meaningful economic deterrent, especially for high-value Imt-token trades where the fee saved will exceed the marginal gas cost. This is trivially reproducible and repeatable across all Imt/Nep245 tokens.

## Recommendation
Do not exempt `Nep245`/`Imt` fee computation based on the per-entry `amount` of a single `TokenDiff` line item. Instead, either: (1) apply the fee unconditionally to `Nep245`/`Imt` tokens (drop the `amount > 1` condition, keeping the `amount <= 1` exemption only for `Nep171`, which is inherently limited to amount 1), or (2) if the exemption is meant to avoid fee-rounding dust on true single-unit MT transfers, compute it against the aggregated absolute delta per token across the whole intent batch/payload rather than per individual `TokenDiff` entry, so splitting into many sub-intents cannot change the total fee charged.

## Proof of Concept
```rust
// tests/src/tests/defuse/intents/token_diff_imt_fee_bypass.rs
// Using EnvBuilder::default().imt().fee(Pips::from_percent(1)) (or similar non-zero fee)
//
// Setup: user1 mints/holds 1000 units of an Imt token (imt_token), user2 holds
// a counter-asset (e.g. ft2) to trade against.
//
// Case A ("single diff"):
//   Sign ONE payload for user1 containing a single TokenDiff:
//     TokenDiff { diff: { imt_token: -1000, ft2: +closure_delta(ft2, 1000, fee) } }
//   Sign matching payload for user2 with opposing deltas.
//   Execute both in one batch via execute_intents.
//   Assert: fees_collected (read from TokenDiffEvent, or fee_collector's imt_token
//   balance) == fee.fee_ceil(1000) > 0.
//
// Case B ("1000 unit diffs"):
//   Sign ONE payload for user1 containing 1000 TokenDiff sub-intents, each:
//     TokenDiff { diff: { imt_token: -1, ft2: +closure_delta(ft2, 1, fee) } }
//   Sign matching payload(s) for user2 with 1000 opposing +1/-k legs (or a single
//   aggregate counter-leg, since TransferMatcher only needs net balance to cancel).
//   Execute both in one batch via execute_intents.
//   Assert: fees_collected (fee_collector's imt_token balance delta) == 0.
//
// Final assertion:
assert_ne!(case_a_fee_collected_imt, case_b_fee_collected_imt);
// case_a_fee_collected_imt == fee.fee_ceil(1000) (> 0)
// case_b_fee_collected_imt == 0
// demonstrating the fee for identical aggregate Imt volume (1000) diverges based on
// how the trade is split into TokenDiff intents within the same signed batch.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L41-78)
```rust
impl ExecutableIntent for TokenDiff {
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
        if self.diff.is_empty() {
            return Err(DefuseError::InvalidIntent);
        }

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L260-283)
```rust
    #[inline]
    pub fn add_delta(&mut self, owner_id: AccountId, token_id: TokenId, delta: i128) -> bool {
        self.0.entry_or_default(token_id).add_delta(owner_id, delta)
    }

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

**File:** crates/primitives/token-id/src/imt.rs (L1-60)
```rust
use std::{fmt, str::FromStr};

use near_account_id::AccountId;

use crate::{TokenIdType, error::TokenIdError};

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
}

impl std::fmt::Debug for ImtTokenId {
    #[inline]
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.minter_id, self.token_id)
    }
}

impl std::fmt::Display for ImtTokenId {
    #[inline]
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self, f)
    }
}

impl FromStr for ImtTokenId {
    type Err = TokenIdError;

    fn from_str(data: &str) -> Result<Self, Self::Err> {
        let (minter_id, token_id) = data
            .split_once(':')
            .ok_or(strum::ParseError::VariantNotFound)?;
        Ok(Self::new(minter_id.parse::<AccountId>()?, token_id))
    }
}

impl From<&ImtTokenId> for TokenIdType {
    #[inline]
    fn from(_: &ImtTokenId) -> Self {
        Self::Imt
    }
}
```
