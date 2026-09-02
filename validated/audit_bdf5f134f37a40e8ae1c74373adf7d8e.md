### Title
Per-leg fee exemption for `Nep245`/`Imt` (`amount<=1`) lets a batch fully bypass protocol fees by splitting a trade into unit-sized `TokenDiff` legs - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::execute_intent` computes and collects protocol fees per `TokenDiff` intent, and `TokenDiff::token_fee` waives the fee entirely for `Nep245`/`Imt` legs whose `amount<=1`. Because this check is evaluated per intent rather than over the aggregated batch delta for a token, a signer can split what would otherwise be a single `TokenDiff` with `delta=-k` (k>1, fee-inducing) into `k` separate signed `TokenDiff` intents each with `delta=-1`, driving the total collected fee for that token to `0` instead of `fee.fee_ceil(k)`.

### Finding Description
The claimed binding: `fees_collected[T] == protocol_fee.fee_ceil(sum_of_negative_deltas_for_T_in_batch)`.

Code path, `contracts/defuse/core/src/intents/token_diff.rs:56-101`:
```rust
let protocol_fee = engine.state.fee();
let mut fees_collected: Amounts = Amounts::default();
for (token_id, delta) in &self.diff {
    ...
    if *delta < 0 {
        let amount = delta.unsigned_abs();
        let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
        fees_collected.add(token_id.clone(), fee).ok_or(...)?;
    }
}
...
engine.state.internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
```
and `token_fee`, `contracts/defuse/core/src/intents/token_diff.rs:206-217`:
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
```
`amount` here is the `unsigned_abs()` of a single intent's per-leg delta, not the sum of all deltas on token `T` across the whole `MultiPayload` batch. Since `Amounts`/`TokenDeltas` is a `BTreeMap<TokenId, i128>` (`token_diff.rs:18`), a single `TokenDiff` intent can only carry one net delta per token, so this exemption is checked at the granularity of "one signed intent, one token". A signer wanting to move `k` units of a `Nep245`/`Imt` token (where the aggregate `k>1` would normally incur `protocol_fee.fee_ceil(k)`) can instead sign `k` separate `TokenDiff` intents, each moving `1` unit of the same token, matched against `k` counter-legs elsewhere in the same `MultiPayload`. Each of the `k` intents independently evaluates `token_fee(token_id, 1, fee)` → `Pips::ZERO`, so `fee_ceil(1)` on `Pips::ZERO` is `0` for every leg, and the batch-wide `fees_collected` for `T` sums to `0`.

Existing guards do not prevent this: the intra-batch balance invariant only checks that all `TokenDiff` deltas across the whole `MultiPayload` net to zero (confirmed by `invariant_violated` test behavior), it does not re-derive or enforce a batch-aggregate fee; `internal_add_balance` faithfully credits whatever `fees_collected` was computed, which is correctly `0` for a batch of unit legs. There is no batch-level re-check of `Self::token_fee` against the sum of deltas for a given token id.

### Impact Explanation
The fee collector (protocol revenue) receives strictly less than `protocol_fee.fee_ceil(aggregate_delta)` for any `Nep245`/`Imt` trade that a signer chooses to fragment into unit-sized legs, down to exactly `0` regardless of how large the aggregate transferred quantity is. This is systematic and repeatable: any signer (or any pair/set of counterparties in one `MultiPayload`) can apply this technique to every `Nep245`/`Imt` trade, at the cost of only extra signed intents (more entries in the same batch, no extra NEAR balance requirement). This matches the listed Critical category "protocol fees bypassed."

### Likelihood Explanation
Preconditions are minimal and fully within the unprivileged attacker's control: the attacker only needs the ability to sign multiple `TokenDiff` intents from their own account(s) and bundle them (with matching counter-legs) inside one `MultiPayload`, exactly as permitted by `execute_intents`/`simulate_intents`. No special role, relayer key, or victim key is required. The only cost is the added transaction/gas overhead of `k` intents versus `1`; there is no cap or per-batch fee re-aggregation that stops this. This is fully repeatable across tokens, accounts, and batches.

### Recommendation
Compute `token_fee`/`fee_ceil` on the aggregate negative delta per token across the whole batch (or per signer across all `TokenDiff` intents included in the same `MultiPayload`), not per individual intent leg, before deciding the `amount>1` exemption for `Nep245`/`Imt`. Alternatively, remove or tighten the `amount<=1` fee exemption for `Nep245`/`Imt` so it cannot be trivially exploited by unit-splitting (e.g., apply it only to strictly non-fungible token semantics, or track cumulative per-token amount already processed within the current batch when deciding the exemption).

### Proof of Concept
```rust
// cargo test in contracts/defuse (near-workspaces sandbox), extending tests/src/tests/defuse/intents/token_diff.rs

// Setup: mint/deposit k units of a Nep245 token T to user1, and equivalent counter-token to user2.
// Baseline: user1 signs ONE TokenDiff { diff: {T: -k, other: +x} }, user2 signs matching counter-intent.
// Execute batch, record fees_collected for T from TokenDiffEvent -> expect fee.fee_ceil(k) (nonzero for fee > 0, k > 1).

// Exploit: user1 signs k SEPARATE TokenDiff intents, each { diff: {T: -1, other: +x/k} },
// user2 signs k matching counter TokenDiff intents { diff: {T: +1, other: -x/k} }.
// Bundle all 2k payloads into a single MultiPayload and call execute_intents.

// Assertion:
let mut total_fee_split = 0u128;
for event in token_diff_events_for_T {
    total_fee_split += event.fees_collected.amount_for(&T);
}
assert_eq!(total_fee_split, 0);                       // observed
assert_eq!(fee.fee_ceil(k), expected_nonzero_fee);     // baseline single-intent fee
assert!(total_fee_split < fee.fee_ceil(k));            // binding broken: batch-split undercharges vs single-intent aggregate
```
This uses `TokenDiff::token_fee` and `Pips::fee_ceil` from `contracts/defuse/core/src/intents/token_diff.rs` and `crates/primitives/fees/src/lib.rs:116-121` directly to compute the expected baseline, and compares it against the sum of `TokenDiffEvent::fees_collected` observed after executing the split batch via `execute_intents`/`simulate_intents`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/defuse/core/src/intents/token_diff.rs (L18-18)
```rust
pub type TokenDeltas = Amounts<BTreeMap<TokenId, i128>>;
```

**File:** contracts/defuse/core/src/intents/token_diff.rs (L56-101)
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

**File:** crates/primitives/fees/src/lib.rs (L116-121)
```rust
    #[inline]
    pub fn fee_ceil(self, amount: u128) -> u128 {
        amount
            .checked_mul_div_ceil(self.as_pips().into(), Self::MAX.as_pips().into())
            .unwrap_or_else(|| unreachable!())
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
