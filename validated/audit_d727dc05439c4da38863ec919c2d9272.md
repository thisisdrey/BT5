This vulnerability is confirmed. Let me lay out the equality binding and the proof.

**Binding claimed:** For any batch of `TokenDiff` intents from a signer, the fee credited to `fee_collector` for token `T` should equal `Pips::fee_ceil` applied to `Σ|negative deltas of T|` — i.e. `fee(T) = fee_ceil(Σ negative_delta_of_T)`, independent of how the negative deltas of `T` are partitioned across separate `TokenDiff` intents in the batch.

**What the code actually does:** `TokenDiff::token_fee` is invoked per-intent, per-token, using only that single intent's `|delta|`, and the calling site `TokenDiff::execute_intent` computes/collects fee separately for each `TokenDiff` object it processes: [1](#0-0) [2](#0-1) 

For `TokenIdType::Nep245` (and `Imt`), fee is only taken when the *single intent's* `amount > 1`; for `amount == 1` it returns `Pips::ZERO`. Since `DefuseIntents` executes a `Vec<Intent>` sequentially with no cross-intent aggregation of deltas before fee computation: [3](#0-2) 

an attacker who signs one `MultiPayload`/`DefuseIntents` containing two `TokenDiff` intents, each `{T: -1}`, pays zero fee on token `T`, whereas one `TokenDiff` intent with `{T: -2}` would pay `fee_ceil(2)`. Both settle identical net token movement (−2 of `T` from signer, credited nowhere but fee_collector in one case), so the fee-collector balance diverges purely based on intent partitioning, not on economic substance.

**Root cause:** the per-unit exemption (`amount > 1` guard) intended to avoid fees on NFT-like single-unit transfers is evaluated on a per-`TokenDiff`-object basis rather than on the aggregate negative delta of the token across the whole batch/signed message.

**Guards that don't help:** `MultiPayload::verify`, nonce/salt checks, and `internal_apply_deltas` all operate correctly and don't touch fee logic; they don't prevent an attacker from encoding one logical trade as N separate `TokenDiff` intents inside the same signed `DefuseIntents.intents` vector (single nonce, single signature) or across multiple separately-signed `MultiPayload`s in the same `execute_intents` batch.

### Title
Protocol fee bypass via `TokenDiff` splitting on `Nep245`/`Imt` unit-exempt fee guard - (File: contracts/defuse/core/src/intents/token_diff.rs)

### Summary
`TokenDiff::token_fee` exempts `Nep245`/`Imt` tokens from fees only when a single intent's `|delta|` is `<= 1`. Because fee is computed per-`TokenDiff` intent rather than on the aggregate negative delta of a token across the batch, an attacker can split a `-N` diff into `N` separate `-1` diffs (in one signed payload or across payloads) and pay zero protocol fee instead of `fee_ceil(N)`.

### Finding Description
The binding that should hold is `fee(T) == Pips::fee_ceil(Σ negative_delta_of_T across the batch)`. In `TokenDiff::execute_intent` (`contracts/defuse/core/src/intents/token_diff.rs:59-78`), fee is computed independently for each `TokenDiff` object via `Self::token_fee(token_id, amount, protocol_fee).fee_ceil(amount)` where `amount` is only that intent's `|delta|`. `token_fee` (`token_diff.rs:206-216`) exempts `Nep245`/`Imt` when `amount <= 1`. `DefuseIntents::execute_intent` (`contracts/defuse/core/src/intents/mod.rs:97-113`) iterates over `Vec<Intent>` and calls each `TokenDiff::execute_intent` independently, with no cross-intent tracking of cumulative deltas for fee purposes. An attacker who deploys their own NEP-245 contract, deposits ≥2 units of a token id into the Verifier, then signs a single `MultiPayload` containing two `TokenDiff` intents `{T: -1}` (or splits across two separately signed payloads in one `execute_intents`/`simulate_intents` batch) causes `token_fee` to return `Pips::ZERO` both times, versus one `TokenDiff{T: -2}` which hits the `amount > 1` branch and pays `fee_ceil(2)`. Net token movement to/from the signer and Verifier custody is identical in both cases; only the fee_collector credit differs. No existing guard (`verify`, nonce/salt checks, `internal_apply_deltas`) inspects or aggregates deltas of the same token across multiple `TokenDiff` intents for fee purposes.

### Impact Explanation
Protocol fees are under-collected for `Nep245`/`Imt` token trades whenever the negative delta is split into unit-sized (`|delta|<=1`) `TokenDiff` intents. This is repeatable indefinitely: any negative delta `N` can be fully de-fee'd by splitting it into `N` unit intents in the same or across payloads, at zero additional cost beyond larger message size. This directly matches the Critical category "protocol fees bypassed" — the victim is the protocol's `fee_collector`, and the blast radius covers all `Nep245`/`Imt`-typed tokens traded via `TokenDiff`.

### Likelihood Explanation
The attacker only needs to control their own NEP-245 contract and token id and deposit ≥2 units into the Verifier for themselves — both fully within an unprivileged attacker's capability. No role, relayer key, or victim key is required. Constructing a `MultiPayload` with multiple `TokenDiff` intents (or multiple payloads in one batch) is a standard client-side operation with normal signing. This is trivially and cheaply repeatable per trade.

### Recommendation
Aggregate negative deltas per `TokenId` across all `TokenDiff` intents in the batch (or at least within a single `DefuseIntents.intents` and across the whole `execute_signed_intents` call) before applying the `TokenIdType::Nep245 | TokenIdType::Imt` unit exemption in `token_fee`, so the `amount > 1` check reflects the true aggregate negative delta of that token rather than a single intent's delta.

### Proof of Concept
Write a `cargo test` in `contracts/defuse/core` (or a `near-workspaces` sandbox test under `tests/`) that:
1. Sets up a signer account with a Verifier balance of `2` units of a `Nep245` token id `T` and a nonzero `protocol_fee`.
2. Path A: builds one `MultiPayload` containing a single `TokenDiff{ T: -2 }` intent, executes via `execute_signed_intents`, and records `fees_collected` for `T` credited to `fee_collector` — expect `fee_ceil(2) > 0`.
3. Path B: builds one `MultiPayload` containing two `TokenDiff{ T: -1 }` intents (or two separately signed payloads with fresh nonces in one batch), executes via `execute_signed_intents`, and records `fees_collected` for `T` — expect `0`.
4. Assert `fees_collected_A != fees_collected_B` despite identical net `-2` movement of `T`, proving the fee depends on intent partitioning rather than aggregate negative delta.

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
