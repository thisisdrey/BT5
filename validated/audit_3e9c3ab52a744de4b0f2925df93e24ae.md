### Title
Protocol fees on Nep245/Imt token transfers can be bypassed by splitting a single large `TokenDiff` delta into multiple unit-sized (`|delta| == 1`) `TokenDiff` intents in one `MultiPayload` - (contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::execute_intent` computes the fee for each negative delta independently via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)`, and `token_fee` explicitly returns `Pips::ZERO` for `Nep245`/`Imt` tokens whenever the per-intent `amount <= 1`. Because fee computation is scoped to a single intent's delta rather than to the aggregate negative delta a signer moves for a given token within a batch, a signer can decompose one large negative delta into many `-1` legs across multiple `TokenDiff` intents in the same `MultiPayload`, causing the total collected fee to be `0` instead of `fee_ceil(protocol_fee, N)`.

### Finding Description
The binding that should hold is: for a signer moving a total negative amount `N` of a fee-eligible token `T` in a batch, `sum(fees_collected[T])` over all intents in that batch should equal `Pips::fee_ceil(protocol_fee, N)`. Instead, the code computes fee per-intent-leg: [1](#0-0) 

and `token_fee` zeroes the fee for `Nep245`/`Imt` types whenever that leg's own `amount <= 1`: [2](#0-1) 

`execute_intent` applies the delta directly to the signer's own account via `internal_apply_deltas`, and the `TransferMatcher`/`Deltas::finalize` mechanism only nets deltas into `Transfers` across accounts/intents in the batch after balances are already changed — it does not re-evaluate or aggregate the fee amount: [3](#0-2) 

Because each `TokenDiff` intent is a fully signed, independent structure (`diff: BTreeMap<TokenId, i128>`), a signer can put a single `-N` leg on token `T` inside one intent (paying `fee_ceil(protocol_fee, N)`), or instead sign `N` separate `TokenDiff` intents each carrying `-1` on the same Nep245/Imt `token_id` (each paying `token_fee(..., 1, ...) = Pips::ZERO`), balancing every leg with corresponding positive/negative deltas on other tokens so each individual intent is self-consistent and the whole batch still nets to zero via `TransferMatcher::finalize`. No existing guard (`MultiPayload::verify`, nonce checks, `TransferMatcher::finalize`, `assert_one_yocto`) inspects the *sum* of `|delta|` for a given `(signer, token_id)` pair across intents in the batch, so this decomposition is not detected or penalized.

### Impact Explanation
The fee that should have been credited to `engine.state.fee_collector()` for token `T` is under-collected — specifically, it collects `0` fees where `fee_ceil(protocol_fee, N)` was expected. This is a direct instance of "protocol fees bypassed," matching the Critical severity bucket in the rules. The attack is repeatable by any signer, for any Nep245/Imt token they control, and scales linearly with the number of unit-sized intents they are willing to sign (cost is purely off-chain signing effort plus per-intent gas/storage for nonce commitment), with no cap preventing arbitrarily large aggregate transfers from evading the fee entirely.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs (a) a balance of a Nep245/Imt token in the Verifier and (b) the ability to sign multiple `TokenDiff` intents with distinct nonces bundled in one `MultiPayload` (or submitted across payloads) — both of which are available to any unprivileged signer per the threat model. No role, relayer key, or victim key is required. The only cost is the linear overhead of constructing/signing `N` intents instead of `1`, which is a purely off-chain, attacker-controlled cost with no per-unit monetary cost, making this highly feasible.

### Recommendation
Compute and reserve the fee based on the aggregate negative delta per `(signer_id, token_id)` across the whole batch (e.g., accumulate net deltas per token before applying `token_fee`/`fee_ceil`), or remove/tighten the `amount <= 1` exemption for `Nep245`/`Imt` so it only applies to genuinely non-fungible token IDs (as opposed to any multi-token balance ≤ 1 within a single intent leg), and enforce the exemption based on the total signer-level exposure for that token across the `MultiPayload`, not per isolated intent.

### Proof of Concept
```rust
// cargo test in contracts/defuse/core (unit-level) or a near-workspaces sandbox test:
// 1. Construct a fungible-style Nep245 token (e.g. wrapped FT via MT) and give signer
//    a balance of N (e.g. N = 1000) plus enough of a counter-token to balance legs.
// 2. Case A ("honest"): sign ONE TokenDiff intent with delta = -1000 on token T
//    (and a balancing +delta elsewhere). Execute via execute_intents.
//    Assert: fees_collected[T] == Pips::fee_ceil(protocol_fee, 1000) (> 0 for fee > 0).
// 3. Case B ("split"): sign 1000 separate TokenDiff intents, each with delta = -1 on
//    token T (and balancing +1 legs on other tokens so each intent's diff is self
//    consistent), batched in one MultiPayload. Execute via execute_intents.
//    Assert: total fees_collected[T] across all TokenDiffEvent occurrences == 0.
// 4. Assert Case A fee != Case B fee (0), demonstrating the aggregate fee binding
//    sum(fees_collected[T]) == fee_ceil(protocol_fee, N) is violated for Case B.
```

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

**File:** contracts/defuse/core/src/engine/state/deltas.rs (L233-283)
```rust
/// Accumulates internal deposits and withdrawals on different tokens
/// to match transfers using `.finalize()`
///
/// Transfers in `TokenDiff` intents are represented as deltas without receivers.
/// This struct accumulates tokens all transfers, and converts them from deltas, to
/// a set of transfers from one account to another.
/// Note that this doesn't touch account balances. The balances were already changed
/// in an earlier stage while executing the intent.
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
```
