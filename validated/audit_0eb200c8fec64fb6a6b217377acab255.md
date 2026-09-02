## Title
Fee-exempt classification of self-issued NEP-245 unit tokens allows unprivileged signers to bypass `TokenDiff` protocol fees - ([File: contracts/defuse/core/src/intents/token_diff.rs])

## Summary
`TokenDiff::token_fee` waives protocol fees on any `TokenIdType::Nep245`/`Imt` leg whenever the per-intent `|delta| <= 1`, based purely on which `TokenId` enum variant was constructed, not on the token's actual economic value. Because `mt_on_transfer` derives the NEP-245 "contract" component of a `TokenId` from `env::predecessor_account_id()` with no whitelist or registration check, any unprivileged signer can permissionlessly mint themselves an arbitrary number of fictitious, unit-valued NEP-245 balances and route economically fungible value through them, paying zero fee on the corresponding `TokenDiff` legs where an equivalent NEP-141 deposit of the same value would always pay `fee.fee_ceil(amount)`.

## Finding Description
The broken binding: for the same signer, the same economic value `V`, and the same protocol fee `f`, the fee collected on a negative `TokenDiff` delta of magnitude `V` should be `f.fee_ceil(V)` regardless of which token standard was used to represent that value. In practice:

```
fee(Nep141, V)                 = f.fee_ceil(V)              // > 0 for f > 0, V > 0
fee(Nep245, split into V units) = Σ f.fee_ceil for V legs of amount=1 = 0
```

Root cause is `TokenDiff::token_fee`: [1](#0-0) 

The classification is derived solely from the `TokenId` variant (`token_id.into(): TokenIdType`), which in turn is set at deposit time based purely on which receiver hook was invoked (`ft_on_transfer` → `Nep141`, `mt_on_transfer` → `Nep245`). There is no economic distinction enforced between a "real" NFT/MT item and a fungible asset artificially wrapped as many unit-valued MT token IDs.

Critically, the "contract" part of a `Nep245TokenId` is taken directly from the caller (`env::predecessor_account_id()`) in `mt_on_transfer`, with no requirement that the calling account actually be a legitimate, previously-registered multi-token contract: [2](#0-1) 

This lets any unprivileged signer call `mt_on_transfer` directly (or via a trivial self-deployed MT contract) and mint themselves balances under `Nep245TokenId::new(<their_account_or_contract>, <arbitrary_token_id_string>)`, exactly as demonstrated by an existing test comment: "Deposit a fictitious token... This is possible because `mt_on_transfer` creates a token from any contract, where the token id... comes from the caller account id" [3](#0-2) . `Contract::deposit` performs no restriction on `TokenId::Nep245`/`Nep141` beyond overflow checks (only `Nep171` NFTs are limited to a single unit) [4](#0-3) .

Exploit flow: the attacker crafts `V` distinct `Nep245TokenId` strings (e.g., `mywrap.near:unit1` .. `mywrap.near:unitV`), deposits amount `1` for each via `mt_on_transfer`, then signs a `MultiPayload` with `TokenDiff` intents containing `V` legs each with `delta = -1` on a distinct unit token, paired against a counterparty's genuine NEP-141 `TokenDiff` leg of matching value. `TransferMatcher::finalize` only enforces that the aggregate deltas across the whole batch net to zero per token id [5](#0-4) ; it does not, and cannot, correct the fee computed per-leg in `TokenDiff::execute_intent`, which is applied token-by-token before finalization: [6](#0-5) 

Because each leg has `amount == 1`, `token_fee` returns `Pips::ZERO` for every one of the attacker's `V` legs, while an equivalent single NEP-141 deposit-and-trade of the same total value `V` would compute `Self::token_fee(Nep141, V, fee) = fee` and collect `fee.fee_ceil(V) > 0`. No existing guard (`verify`, nonce/salt checks, `assert_one_yocto`, `#[pause]`, access-control roles) touches this path — they authenticate the signer and message, not the semantic validity of the token classification.

## Impact Explanation
This under-collects protocol fees that are otherwise mandatory on every NEP-141 `TokenDiff` trade, matching the "protocol fees bypassed or over-collected" Critical category. The attacker keeps the fee amount that would otherwise be transferred to `fee_collector` on every trade routed through self-issued unit-valued NEP-245 wrappers, at the expense of protocol revenue (borne collectively by the fee collector / protocol treasury, not any specific victim's custodied balance — no other user's funds are moved without authorization). It is fully repeatable: any signer can mint unlimited fictitious `Nep245TokenId`s and structure arbitrarily large `V` into `V` unit legs, applying to every future trade they make.

## Likelihood Explanation
Preconditions are trivial: the attacker needs only a NEAR account (no roles, no special key), can call `mt_on_transfer` on the Defuse contract directly with themselves as `predecessor_account_id`, and needs a real counterparty (or their own second account) willing to trade the other leg — the same requirement any normal `TokenDiff` swap already has. Cost scales with `V` (one deposit + one `TokenDiff` leg per unit), which is a usability/gas tradeoff for the attacker, not a blocker; a sufficiently high-value trade split into a moderate number of unit legs remains cheap relative to the fee saved for large `f * V`.

## Recommendation
Do not derive fee exemption purely from the `TokenId` variant. Either (a) remove the NEP-245/IMT `|delta| <= 1` fee exemption entirely and charge fee uniformly regardless of standard, or (b) restrict the exemption to token contracts/ids that are verifiably non-fungible/non-splittable (e.g., require an allowlist of trusted external MT contracts, or apply the exemption only when the deposited MT contract enforces true NFT-style supply-of-1-per-id semantics globally, not just per leg), so that fee treatment reflects the token's actual fungibility rather than the depositor's chosen wrapping and unit-splitting strategy.

## Proof of Concept
```rust
// cargo test (sandbox/near-workspaces), contracts/defuse core + tests crate

// Path A: NEP-141 baseline
// 1. env.defuse_ft_deposit_to(ft.contract_id(), V, attacker, None)
// 2. attacker signs TokenDiff { diff: [(Nep141TokenId(ft), -V), (counterparty_token, +X)] }
// 3. counterparty signs matching TokenDiff closing the deltas
// 4. execute batch; assert fees_collected.amount_for(Nep141(ft)) == fee.fee_ceil(V) > 0

// Path B: NEP-245 self-issued unit wrap
// 1. For i in 0..V: attacker calls mt_on_transfer(predecessor=attacker_or_own_mt_contract,
//    token_ids=["unit{i}"], amounts=[1], msg=<deposit msg for attacker>)
//    -> mints Nep245TokenId(attacker_contract, "unit{i}") balance = 1 to attacker
// 2. attacker signs one TokenDiff with V legs: [(Nep245("unit0"), -1), ..., (Nep245("unit{V-1}"), -1),
//    (counterparty_token, +X)]
// 3. counterparty signs matching TokenDiff with the V reciprocal +1 legs and -X leg
// 4. execute batch; assert fees_collected is empty / total fee == 0, despite total value moved == V

// Assertion: fee_A (Path A) > 0 == fee.fee_ceil(V), fee_B (Path B) == 0, for identical V and fee schedule.
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

**File:** contracts/defuse/src/contract/tokens/nep245/deposit.rs (L19-55)
```rust
    fn mt_on_transfer(
        &mut self,
        sender_id: AccountId,
        previous_owner_ids: Vec<AccountId>,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        msg: String,
    ) -> PromiseOrValue<Vec<U128>> {
        let token = env::predecessor_account_id();

        require!(!amounts.is_empty(), "invalid args");

        require!(
            token_ids.len() == amounts.len(),
            "NEP-245: Contract MUST panic if `token_ids` length does not equals `amounts` length"
        );

        require!(
            previous_owner_ids.len() == token_ids.len(),
            "NEP-245: Contract MUST panic if `previous_owner_ids` length does not equals `token_ids` length"
        );

        require!(
            token != env::current_account_id(),
            "self-wrapping is not allowed"
        );

        let core_token_ids = token_ids
            .iter()
            .inspect(|token_id| {
                if token_id.len() > MAX_TOKEN_ID_LEN {
                    DefuseError::TokenIdTooLarge(token_id.len()).panic();
                }
            })
            .cloned()
            .map(|token_id| Nep245TokenId::new(token.clone(), token_id))
            .map(Into::into);
```

**File:** tests/src/tests/defuse/tokens/nep245/mt_transfer_resolve_gas.rs (L150-153)
```rust
    // Deposit a fictitious token, nep245:user.test.near:<token-id>, into defuse.
    // This is possible because `mt_on_transfer` creates a token from any contract,
    // where the token id (first part, the contract id part), comes from the caller
    // account id.
```

**File:** contracts/defuse/src/contract/tokens/mod.rs (L46-59)
```rust
            let total_supply = self
                .storage
                .state
                .total_supplies
                .add(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            match token_id {
                TokenId::Nep171(ref tid) => {
                    if total_supply > 1 {
                        return Err(DefuseError::NftAlreadyDeposited(tid.clone()));
                    }
                }
                TokenId::Nep141(_) | TokenId::Nep245(_) | TokenId::Imt(_) => {}
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
