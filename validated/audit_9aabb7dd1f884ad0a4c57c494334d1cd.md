### Title
Fee exemption applied per-`TokenDiff` instance instead of per net trade lets NEP-245/IMT fees be bypassed by splitting a large transfer into many `amount == 1` legs - ([File: contracts/defuse/core/src/intents/token_diff.rs])

### Summary
`TokenDiff::token_fee` waives the protocol fee whenever a single `TokenDiff`'s `amount <= 1` for `Nep245`/`Imt` token types, intended to spare NFT-like transfers from fees. Because this check is evaluated independently for every `TokenDiff::execute_intent` call rather than on the net traded amount of a token across a signed batch, a signer can split a transfer of `N` fungible units of a NEP-245 sub-token into `N` separate `TokenDiff` intents each with `|delta| == 1`, making every leg hit the `amount > 1` false branch and return `Pips::ZERO`, so no fee is ever collected on `N` units of real value.

### Finding Description
The broken binding is: **fee owed on total NEP-245 value transferred == fee charged should be independent of how many `TokenDiff` intents the transfer is split into**. In the code this does not hold.

In `contracts/defuse/core/src/intents/token_diff.rs`, `execute_intent` computes and collects a fee independently per intent: [1](#0-0) 
and `token_fee` decides the fee purely from the `amount` passed for that single call: [2](#0-1) 

The comment "do not take fees on NFTs and MTs with `|delta| <= 1`" reflects an assumption that a `TokenDiff` with `amount == 1` on a NEP-245/IMT token represents a genuine non-fungible transfer. However, NEP-245 (`Nep245TokenId`) is a multi-token standard where a single `(contract, token_id)` can be fungible with a large supply; nothing in `TokenIdType` distinguishes an intrinsically-NFT sub-token from a fungible one being moved one unit at a time. Since `token_fee` only looks at the `amount` argument of the current call, and `execute_intent` never aggregates deltas for the same `token_id` across multiple `TokenDiff` intents in the same `MultiPayload`, a signer can construct N separate `TokenDiff`s (or use N intents split across payloads combined in one `execute_intents`/`simulate_intents` batch) each moving exactly 1 unit of the same `(contract, token_id)`, with a matching counterparty absorbing the same legs. Each call to `token_fee` independently observes `amount == 1` and returns `Pips::ZERO`.

Downstream, `Deltas`/`TransferMatcher::finalize` (contracts/defuse/core/src/engine/state/deltas.rs) only checks that deposits and withdrawals net to zero across the whole batch — it has no concept of fees and does not re-derive or enforce a minimum fee based on aggregated per-token volume: [3](#0-2) 
So conservation of balances holds (no funds are stolen), but the `fees_collected` amount accumulated in `execute_intent` and credited to `engine.state.fee_collector()` for that token stays `0` regardless of how large the aggregate transferred volume is.

### Impact Explanation
This does not move funds without authorization and does not break the Verifier's balance invariant — it only causes the protocol fee, which the rules explicitly list as a Critical-impact category ("protocol fees bypassed or over-collected"), to be systematically under-collected for any NEP-245/IMT volume that a signer chooses to structure as a sequence of `amount == 1` `TokenDiff` legs instead of one large-`amount` `TokenDiff`. The fee collector (`engine.state.fee_collector()`) receives strictly less than the protocol fee schedule (`engine.state.fee()`) intends for that trade, and this is repeatable by any signer, for any NEP-245/IMT token, in any batch, with no privileged role required. Note this is an economic/fee-accounting bypass, not a funds-theft or double-settlement bug — no counterparty loses tokens beyond the missed fee.

### Likelihood Explanation
The only precondition is that the attacker (and a cooperating or matched counterparty, e.g. themselves via two balances, or a solver-user trade as shown in `solver_user_closure` test pattern) holds a NEP-245 balance with `amount > 1` and is willing to sign/submit `N` `TokenDiff` intents instead of one. This is entirely within the capabilities of an unprivileged signer submitting a `MultiPayload` to `execute_intents`/`simulate_intents`; it requires no special role, no deposit `msg` trick, and no contract deployment. The only cost is proportionally larger transaction size/gas for `N` intents instead of one, which the rules explicitly exclude from scope ("unbounded gas or storage consumption... rate limiting... resource exhaustion" is out of scope, but is only the cost side here, not the vulnerability itself). This makes the bypass fully feasible and repeatable across accounts, tokens, and batches, limited only by batch size/gas, which is a soft, non-security constraint rather than a code-enforced barrier.

### Recommendation
Compute the fee-exemption eligibility on the *net* amount of a given `(token_id)` moved by a signer across the whole batch/payload (or track cumulative per-signer per-token amounts across all `TokenDiff` intents processed within one `execute_intents` call) rather than on the `amount` of each individual `TokenDiff` instance. Alternatively, restrict the `amount <= 1` NFT/MT fee exemption to token types that are provably non-fungible (e.g., only `Nep171`, and for `Nep245`/`Imt` require an explicit "max-supply == 1" attestation), so that fungible NEP-245 sub-tokens cannot exploit the exemption regardless of how many legs a transfer is split into.

### Proof of Concept
```rust
// cargo test -p defuse-tests token_diff::split_amount_bypasses_nep245_fee

// 1. Deploy an MT contract; give `solver` a balance of 1000 units of Nep245TokenId(mt.near, "ft1").
// 2. Deposit into Defuse for `solver` (amount = 1000) and for `user` a fee-free counter-token.
// 3. Instead of ONE TokenDiff{ diff: { nep245_token: -1000, other_token: +X } },
//    construct N = 1000 separate TokenDiff intents, each:
//      TokenDiff { diff: { nep245_token: -1, other_token: +X/1000 }, .. }
//    signed by `solver` (and matching +1 legs signed by `user`), all included in one
//    MultiPayload passed to `simulate_intents`/`execute_intents`.
// 4. Assert:
//    a) env.mt_batch_balance_of(fee_collector, [nep245_token]) == U128(0)
//       (fee_collected for nep245_token across the whole batch is zero)
//    b) solver's nep245 balance decreased by exactly 1000 and user's increased by 1000
//       (conservation holds - TransferMatcher::finalize succeeds)
//    c) Contrast: repeat with a SINGLE TokenDiff{ nep245_token: -1000, ... } with fee > 0
//       configured via Env::builder().fee(Pips::ONE_PERCENT) and assert fee_collector's
//       balance for nep245_token > 0, matching TokenDiff::token_fee's `amount > 1` branch.
// This demonstrates that splitting into amount=1 legs collects strictly less fee than a single
// TokenDiff moving the same aggregate amount, contradicting the intended "do not fee NFTs/MTs with |delta|<=1"
// exemption which is documented in contracts/defuse/core/src/intents/token_diff.rs:206-216.
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
