### Title
Fee-splitting across multiple `TokenDiff` intents in one `MultiPayload` bypasses protocol fees - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Finding Description
`TokenDiff::token_fee` at [1](#0-0)  computes the applicable fee **per individual `TokenDiff` intent leg**, using only that leg's own `amount = delta.unsigned_abs()`:

```
TokenIdType::Nep245 | TokenIdType::Imt if amount > 1 => {}
TokenIdType::Nep171 | TokenIdType::Nep245 | TokenIdType::Imt => return Pips::ZERO,
```

This is invoked from `TokenDiff::execute_intent` per negative-delta leg of a single `TokenDiff` struct: [2](#0-1) .

Crucially, a signer's `DefusePayload` carries `DefuseIntents { intents: Vec<Intent> }` — an ordered list of independent intents, all covered by a **single signature and nonce** [3](#0-2) . `DefuseIntents::execute_intent` iterates this vector and executes each `Intent` (each of which may be a separate `TokenDiff`) completely independently: [4](#0-3) . `Engine::execute_signed_intent` extracts this single payload, verifies it once, commits one nonce, then calls `intents.execute_intent(...)` which fans out to each `Intent::execute_intent` [5](#0-4) .

Because `token_fee` never sees the aggregate `|delta|` across all `TokenDiff` intents in the batch — only the per-intent, per-leg `amount` — an attacker can split what is economically a single `-200` Nep245 (MT-style) leg into 200 separate `TokenDiff` intents, each `{Nep245_token: -1, Nep141_token: +X}`, inside one signed `MultiPayload`. Each of the 200 calls to `token_fee(nep245_token_id, amount=1, protocol_fee)` falls into the `amount <= 1` arm and returns `Pips::ZERO`, so `fee_ceil(1) == 0` every time, and `internal_add_balance` to `fee_collector` is never invoked for any of the 200 legs [6](#0-5) .

None of the listed guards (`MultiPayload::verify`, nonce/salt checks, `TransferMatcher::finalize`, `checked_*` arithmetic) address this because they only ensure the *signature is valid* and that the *overall batch nets to zero across counterparties*; they say nothing about fee computation being done on aggregated deltas rather than per-leg deltas. `TransferMatcher::finalize` balances token flows across the whole execution, but fee accounting is computed independently inside each `TokenDiff::execute_intent` call before finalization, so splitting has no effect on the balance invariant while still zeroing out the fee.

The root cause is explicit and intentional in the code comment ("do not take fees on NFTs and MTs with |delta| <= 1"), which was designed for true NFTs/atomic MT items where charging a percentage fee on a unit item is meaningless — but Nep245/IMT token IDs in this protocol are also used to represent divisible/fungible-style balances (e.g., wrapped multi-token balances), so the same exemption can be weaponized by simply issuing `amount=1` legs repeatedly to move an arbitrarily large aggregate quantity fee-free.

### Impact Explanation
This under-collects protocol fees: `fee_collector`'s `AccountState::token_balances` receives `0` instead of `fee_ceil(protocol_fee, Σ|negative deltas of token T|)`. This directly matches the "protocol fees bypassed" Critical impact category — no unauthorized signature is required, the loss is deterministic and fully attacker-controlled (fee due scales with trade size, and can always be reduced to exactly zero regardless of trade size by choosing enough |delta|=1 legs), and it is repeatable across any account, any Nep245/Imt token, and any batch size, with the only cost being additional intents inside one already-signed payload (no extra signature or nonce cost). The counterparty (e.g., a solver) who priced their own closure assuming a nonzero fee is worse off (overpaying relative to actual fee taken), and the protocol operator is shorted fee revenue on every batch that uses this pattern.

### Likelihood Explanation
The attack requires only an unprivileged signer with a Nep245/Imt token balance and any counterparty willing to sign the matching leg (or the same signer's own multi-leg trade, if self-matching is possible) — both are within the stated attacker capability (submit `MultiPayload`, sign with own keys, hold their own balances). No role, relayer key, or DAO permission is needed. It is trivially cheap: constructing 200 `TokenDiff` intents inside a single `DefuseIntents` array costs only gas/payload size (this bug report explicitly excludes gas/storage/DoS concerns from scope, but the fee bypass itself is orthogonal to gas cost and requires no unusual gas expenditure beyond a moderately large but ordinary batch). This is fully reproducible in a deterministic unit test without any timing or network assumptions.

### Recommendation
Compute `token_fee` based on the **aggregate** `|delta|` per `TokenId` across the entire intents batch (or at minimum across the entire `DefuseIntents.intents` list) rather than per-individual-intent, per-leg amounts — e.g., pre-aggregate all `TokenDiff` deltas for the same signer/token before calling `token_fee`, or remove/tighten the "amount <= 1 exempt" rule so it cannot be abused by fee-free unit-splitting of fungible-style Nep245/Imt balances (e.g., only exempt token IDs whose Nep245/Imt token metadata indicates non-fungibility, or track and sum `amount` per `(signer, token_id)` across the whole batch before applying the threshold check).

### Proof of Concept
```
cargo test (near-workspaces sandbox), pseudocode:

1. Set env.builder().fee(Pips::ONE_PERCENT), fee_collector = fee_collector_account.
2. Deposit 200 units of a Nep245 token T (wrapped MT balance) to `attacker`, and enough of Nep141 token X to `counterparty` to match.
3. Build one DefuseIntents with 200 TokenDiff intents, each:
     TokenDiff { diff: { T: -1, X: +x_i }, ... }
   summing to T: -200, X: +ΣX total, and sign as ONE MultiPayload from `attacker`.
4. Build the matching counterparty payload(s) that supply T:+200 / X:-ΣX so TransferMatcher::finalize nets to zero.
5. Call execute_intents with both payloads.
6. Assert:
     - fee_collector's AccountState::token_balances[T] == 0 (actual, per current code)
     - vs the binding: expected == Pips::fee_ceil(Pips::ONE_PERCENT, 200) == 2 (per unit conversion used by fee_ceil in crates/primitives/fees/src/lib.rs)
   Demonstrating actual (0) != expected (>0) fee collected, confirming the bypass.
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

**File:** contracts/defuse/core/src/intents/token_diff.rs (L96-101)
```rust
        // deposit fees to collector
        if !fees_collected.is_empty() {
            engine
                .state
                .internal_add_balance(engine.state.fee_collector().into_owned(), fees_collected)?;
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

**File:** contracts/defuse/core/src/intents/mod.rs (L30-37)
```rust
pub struct DefuseIntents {
    /// Sequence of intents to execute in given order. Empty list is also
    /// a valid sequence, i.e. it doesn't do anything, but still invalidates
    /// the `nonce` for the signer
    /// WARNING: Promises created by different intents are executed concurrently and does not rely on the order of the intents in this structure
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub intents: Vec<Intent>,
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

**File:** contracts/defuse/core/src/engine/mod.rs (L75-82)
```rust
        // commit nonce
        self.verify_intent_nonce(nonce, deadline)?;
        self.state.commit_nonce(signer_id.clone(), nonce)?;

        intents.execute_intent(&signer_id, self, hash)?;
        self.inspector.on_intent_executed(&signer_id, hash, nonce);

        Ok(())
```
