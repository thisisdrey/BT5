### Title
NEP-245 fee-exemption in `TokenDiff::token_fee` is applied per-intent, not per-batch, letting fungible MT transfers be split into `amount == 1` legs to bypass protocol fees - (File: `contracts/defuse/core/src/intents/token_diff.rs`)

### Summary
`TokenDiff::execute_intent` computes the protocol fee for each `(token_id, delta)` pair independently, using `TokenDiff::token_fee`, which returns `Pips::ZERO` for `TokenIdType::Nep245`/`Imt` whenever the *per-intent* `amount <= 1`. Since a `DefuseIntents::intents` batch (or a set of `MultiPayload`s executed together via `execute_intents`) can contain arbitrarily many separate `TokenDiff` intents on the same NEP-245 `token_id`, a signer can split a negative delta of `N` units into `N` separate `TokenDiff` intents each with `delta == -1`, each individually qualifying for the `amount <= 1` fee exemption, paying zero fee in total instead of `fee.fee_ceil(N)`.

### Finding Description
The intended binding (as implemented for a *single* `TokenDiff` intent) is:
`fee_collected(token_id) == TokenDiff::token_fee(token_id, |delta|, protocol_fee).fee_ceil(|delta|)` computed on `delta`, the amount of that one intent's entry for `token_id`.

Code path:
- `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:59-78`) iterates `self.diff` (a `BTreeMap<TokenId, i128>` for a *single* intent) and for each negative delta computes `let fee = Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount);` using only that intent's own `amount = delta.unsigned_abs()`.
- `TokenDiff::token_fee` (`contracts/defuse/core/src/intents/token_diff.rs:206-216`) returns `Pips::ZERO` for `TokenIdType::Nep245 | TokenIdType::Imt` when `amount <= 1` (i.e. the `if amount > 1 => {}` branch is skipped, falling into the `return Pips::ZERO` arm).
- The engine (`contracts/defuse/core/src/engine/mod.rs:32-83` and `contracts/defuse/core/src/intents/mod.rs:97-113`) executes every `Intent` in `DefuseIntents::intents` (and every `MultiPayload` in the batch) independently and sequentially, with no aggregation of deltas per `(signer, token_id)` before fee calculation. `Deltas`/`finalize()` only enforces that the batch's total deltas net to zero across all signers — it never re-derives or re-checks fees.

Because a single signed payload's `DefuseIntents.intents: Vec<Intent>` can hold multiple `TokenDiff` entries, and because `execute_intents`/`simulate_intents` accept `Vec<MultiPayload>`, an attacker (acting as either side of a swap, or coordinating with a counterparty they control) can present two `TokenDiff` intents each with `diff = {token_id: -1}` instead of one `TokenDiff` with `diff = {token_id: -2}`. Each of the two intents independently hits `TokenIdType::Nep245 if amount > 1 => {}`'s false branch (`1` is not `> 1`) and returns `Pips::ZERO`, so `fee_ceil(1) == 0` twice, i.e. `0` total fee collected, vs. a single `-2` delta paying `fee.fee_ceil(2) > 0` (for any `protocol_fee > 0`).

Existing guards do not catch this: `MultiPayload::verify`, nonce/salt checks, and `finalize()`'s balance-matching invariant are all satisfied identically in both cases (the net token movement is the same, only the *fee* differs), so nothing in the reachable path re-aggregates per-token deltas for fee purposes.

### Impact Explanation
Protocol fee revenue is under-collected on NEP-245 token legs of `TokenDiff` swaps: the fee_collector receives strictly less than `protocol_fee.fee_ceil(total_negative_delta)` for any NEP-245 token whose transfer is split into unit-sized legs. This matches the explicitly listed Critical category "protocol fees bypassed or over-collected." The loss scales linearly with the number of `amount == 1` legs the attacker is willing to include in the batch/payload, so an attacker moving `N` units of a fungible-like NEP-245 asset (e.g. a bridged/wrapped FT represented internally as a multi-token) can save up to the entirety of `fee.fee_ceil(N)` by using `N` separate `TokenDiff` intents instead of one. It is repeatable across any NEP-245 `token_id` and any signer/counterparty pair, bounded in practice by per-transaction gas/payload-size limits.

### Likelihood Explanation
Preconditions: a nonzero `protocol_fee`, a NEP-245 `token_id` with balance `>= 2` for the signer, and a counterparty (or matched intent) providing the offsetting positive delta so the batch nets to zero. The attacker only needs to construct multiple `TokenDiff` intents (either within one signed `DefuseIntents.intents` vector or as separate `MultiPayload`s in the same `execute_intents`/`simulate_intents` call) instead of one — no special privileges, roles, or additional cost beyond larger payload/gas usage. This is fully self-serviceable by any unprivileged signer and directly profitable (fee savings), not mere griefing.

### Recommendation
Aggregate negative deltas per `(signer, token_id)` across the entire batch (all `TokenDiff` intents in all `MultiPayload`s of one `execute_intents`/`simulate_intents` call, or at minimum across all `TokenDiff` intents within one signer's `DefuseIntents.intents`) before applying the `amount <= 1` NEP-245/IMT fee exemption and before computing `fee_ceil`, so that fee liability is determined by the true net exposure to a token rather than by how many intents the attacker chooses to split it into.

### Proof of Concept
`cargo test` in `contracts/defuse/core` (or an integration test in `tests/src/tests/defuse/intents/token_diff.rs`) with a nonzero `Pips` fee and a NEP-245 `token_id`:
1. Give signer A a balance of `2` units of a NEP-245 `token_id`, and set up a matching counterparty B able to receive it (mirroring the pattern in `swap_p2p`/`solver_user_closure`).
2. Case 1 ("split"): sign one `MultiPayload` (or two) containing two `TokenDiff` intents from A, each `diff = {token_id: -1}` (with matching `+1` legs from B on some other token to satisfy `finalize()`), execute via `execute_intents`. Assert fee_collector's `mt_balance_of(token_id)` stays `0`.
3. Case 2 ("unsplit"): sign one `TokenDiff` intent from A with `diff = {token_id: -2}` (matched by B), execute via `execute_intents`. Assert fee_collector's `mt_balance_of(token_id)` equals `protocol_fee.fee_ceil(2)` (`> 0`).
4. The discrepancy between step 2 (`0`) and step 3 (`> 0`) for moving the identical total amount `2` of the identical token demonstrates the fee-collection divergence. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** contracts/defuse/core/src/intents/mod.rs (L28-37)
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

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-43)
```rust
#[near]
impl Intents for Contract {
    #[pause(name = "intents")]
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
        {
            // NOTE: Not all `mt_transfer` events are refundable, but it's safe to check them
            // all at once since non-refundable transfers only increase the potential refund
            // log size without affecting correctness. This can actually prevent resolve transfer
            // from failing due to too long event log !!!
            event
                .check_refund()
                .unwrap_or_else(|err| err.panic())
                .emit();
        }
    }

```
