## Title
Protocol fee on `Nep245`/`Imt` negative-delta legs can be bypassed by splitting a large `TokenDiff` amount into many unit (`amount==1`) intents - (`contracts/defuse/core/src/intents/token_diff.rs`)

## Summary
`TokenDiff::execute_intent` computes the fee on a negative delta using `Self::token_fee(token_id, amount, protocol_fee)` where `amount` is **that single intent's own** `delta.unsigned_abs()`, and `token_fee` returns `Pips::ZERO` for `Nep245`/`Imt` token ids whenever `amount <= 1` [1](#0-0) [2](#0-1) . Because `TransferMatcher` only nets raw balance deltas per `(TokenId, owner)` across *all* intents in the batch, independent of how many `TokenDiff` intents contributed to that owner's total [3](#0-2) , an attacker can split one large negative delta into many `delta == -1` legs and pay zero fee on the whole position instead of `fee_ceil` on the aggregate.

## Finding Description
Binding that should hold: for any signer's aggregate negative flow of `N` units on a `Nep245`/`Imt` token `T` with protocol fee `f>0`, the fee collected should equal `fee_ceil(f, N)` (as it does when expressed as one intent, verified by `token_fee(T, N, f)==f` for `N>1`) [2](#0-1) .

What actually happens: `DefuseIntents::intents` is a `Vec<Intent>`, and each `Intent::TokenDiff` is executed independently via `ExecutableIntent::execute_intent`, one call per intent, each with its own fee computation [4](#0-3) . Inside `TokenDiff::execute_intent`, the fee for a negative delta is:
```rust
let amount = delta.unsigned_abs();
let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);
```
using only that single intent's own `delta` [1](#0-0) . `token_fee` explicitly zeroes the fee for `Nep245`/`Imt` when `amount <= 1`:
```rust
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
``` [2](#0-1) 

Meanwhile balance netting is handled entirely separately by `TransferMatcher`, which just accumulates `deposit`/`withdraw` calls per `(owner_id, token_id)` regardless of which or how many intents produced them, then matches deposits against withdrawals in `finalize()` [5](#0-4) . The internal balance mutations that feed `TransferMatcher` (`internal_add_balance`/`internal_sub_balance`) are wired through `Deltas<S>` exactly this way [6](#0-5) .

Exploit: an attacker who owes `N>1` units of a `Nep245`/`Imt` token `T` on the negative side of a swap (whether they are the "solver" or the original requester) signs a single `DefusePayload<DefuseIntents>` with `N` separate `Intent::TokenDiff { diff: {T: -1} }` entries (one nonce, one signature, `N` intent objects) instead of one `Intent::TokenDiff { diff: {T: -N} }`. Each of the `N` unit legs is evaluated by `token_fee(T, 1, f)` → `Pips::ZERO`, so total fee collected for that leg is `0`, while `TransferMatcher` still nets the `N` withdrawals against a counterparty's single `+N` deposit correctly, so the swap still settles as if nothing were wrong. Had the same `N` been submitted as one intent, `token_fee(T, N, f)` would return the real `f` and `fee_ceil(f, N) > 0` would be collected.

None of the existing guards (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`, `checked_*` arithmetic) prevent this because they only protect signature validity and balance-invariant matching — they never re-derive fee from the aggregate per-token flow across intents.

## Impact Explanation
The `fee_collector` under-collects `fee_ceil(f, N)` worth of tokens on every `Nep245`/`Imt` negative leg an attacker chooses to fragment into unit legs, for every trade they route through the Verifier. This is directly "protocol fees bypassed" (Critical category). It is fully repeatable: any account, any `Nep245`/`Imt` `TokenId`, any batch, with the cost being only extra intent objects (no extra gas-limited concern is in scope, but the attack itself needs no more than ordinary transaction gas for a modest `N`). Blast radius is every trade using a Nep245/Imt-typed asset with fee `f>0`.

## Likelihood Explanation
No special privileges are required — the attacker only needs to be a normal signer with a negative delta on a `Nep245`/`Imt` `TokenId` in a swap, and control over how they structure their own signed `DefuseIntents.intents` list (which they always do, since they author and sign it). No cooperation from the counterparty or any privileged role is needed. The only "cost" is submitting `N` intent objects instead of `1` inside the same payload/transaction.

## Recommendation
Compute the fee-eligible `amount` from the **aggregate** negative delta on `(signer_id, token_id)` across the whole batch/payload (e.g., sum deltas per signer+token before calling `token_fee`, or move the `amount>1` exemption check to operate on the netted `TransferMatcher` withdrawal instead of the individual intent's delta), so that splitting one logical position into many `TokenDiff` intents cannot change the fee outcome.

## Proof of Concept
```rust
// contracts/defuse/core/src/intents/token_diff.rs (unit test)
#[test]
fn fee_bypass_by_splitting_nep245_delta() {
    let token_id = TokenId::from(Nep245TokenId::new(
        "mt.near".parse::<AccountId>().unwrap(), "ft1".to_string(),
    ));
    let fee = Pips::ONE_PERCENT;
    const N: u128 = 1000;

    // Binding LHS: fee if expressed as ONE intent with delta = -N
    let fee_single = TokenDiff::token_fee(&token_id, N, fee).fee_ceil(N);
    assert!(fee_single > 0);

    // Binding RHS: fee if the SAME aggregate flow is split into N unit legs
    let fee_split: u128 = (0..N)
        .map(|_| TokenDiff::token_fee(&token_id, 1, fee).fee_ceil(1))
        .sum();
    assert_eq!(fee_split, 0);

    // Equality is broken: aggregate fee owed != fee actually collectible
    assert_ne!(fee_single, fee_split);
}
```
A full end-to-end sandbox test would additionally sign one `DefusePayload<DefuseIntents>` with `N` `Intent::TokenDiff{diff:{T:-1}}` entries matched via `TransferMatcher` against a counterparty's single `+N` `TokenDiff`, execute via `execute_intents`, and assert the `fee_collector`'s post-execution `Nep245` balance for `T` is `0` instead of `fee_ceil(f, N)`.

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L136-164)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        for (token_id, amount) in tokens {
            self.state
                .internal_add_balance(owner_id.clone(), [(token_id.clone(), amount)])?;
            if !self.deltas.deposit(owner_id.clone(), token_id, amount) {
                return Err(DefuseError::BalanceOverflow);
            }
        }
        Ok(())
    }

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L241-284)
```rust
#[derive(Debug, Default)]
pub struct TransferMatcher(HashMap<TokenId, TokenTransferMatcher>);

impl TransferMatcher {
    #[inline]
    pub fn new() -> Self {
        Self(HashMap::new())
    }

    #[inline]
    pub fn deposit(&mut self, owner_id: AccountId, token_id: TokenId, amount: u128) -> bool {
        self.0.entry_or_default(token_id).deposit(owner_id, amount)
    }

    #[inline]
    pub fn withdraw(&mut self, owner_id: AccountId, token_id: TokenId, amount: u128) -> bool {
        self.0.entry_or_default(token_id).withdraw(owner_id, amount)
    }

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
}
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
