### No vulnerability found for this question.

**Reasoning:** The behavior described is explicit, documented, and self-inflicted rather than an authorisation bypass.

- The `AuthCall::attached_deposit` field is explicitly documented: "NOTE: the `wNEAR` will not be refunded in case of fail." [1](#0-0)  This is a publicly known, intended design tradeoff, not a hidden flaw — the same "no refund on failure" behavior is documented identically for `NativeWithdraw`. [2](#0-1) 

- The `signer_id` of an `AuthCall` is always the account that cryptographically signed the intent (per `MultiPayload::verify` / `DefusePayload::signer_id` binding used throughout intent execution) — the rules explicitly state the attacker "signs as themselves" and does not hold the victim's private key. So in this scenario `signer_id == attacker`, meaning any wNEAR debited via `auth_call` in `state.rs` [3](#0-2)  and `Deltas::auth_call` [4](#0-3)  is debited from the attacker's own balance, not a victim's.

- The promise chain `near_withdraw` → `do_auth_call` → `on_auth` is fully attacker-controlled: the attacker deploys the `contract_id` callee, sets `msg`, and chooses to make `on_auth` panic. `do_auth_call` only checks that `near_withdraw` itself succeeded via `promise_result_checked_void(0)`; the subsequent `on_auth` call is a separate, unchecked promise, exactly as the doc comment warns. [5](#0-4) 

- Because the attacker is both the signer whose balance is debited and the deployer of the failing callee, they only destroy their own funds by their own construction of the payload — this is griefing with no attacker profit and no victim whose funds move without authorisation, which is explicitly out of scope per the rules ("Reject... griefing with no attacker profit") and does not match any of the listed Critical/High impact categories (no unauthorised movement of a victim's funds, no double-settlement, no fee bypass, no locked-account bypass).

Consequently, the claimed binding violation does not constitute a valid, in-scope vulnerability.

### Citations

**File:** contracts/defuse/core/src/intents/auth.rs (L26-32)
```rust
    /// Optionally, attach deposit to `on_auth()`
    /// call. The amount will be subtracted from user's NEP-141 `wNEAR`
    /// balance.
    ///
    /// NOTE: the `wNEAR` will not be refunded in case of fail.
    #[serde(default, skip_serializing_if = "NearToken::is_zero")]
    pub attached_deposit: NearToken,
```

**File:** contracts/defuse/core/src/intents/tokens.rs (L429-431)
```rust
/// This will subtract from the account's wNEAR balance, and will be sent to the account specified as native NEAR.
/// NOTE: the `wNEAR` will not be refunded in case of fail (e.g. `receiver_id`
/// account does not exist).
```

**File:** contracts/defuse/src/contract/intents/state.rs (L303-316)
```rust
    fn auth_call(&mut self, signer_id: &AccountIdRef, auth_call: AuthCall) -> Result<()> {
        if auth_call.attached_deposit.is_zero() {
            Self::do_auth_call(signer_id.to_owned(), auth_call)
        } else {
            // withdraw from signer's wNEAR balance
            self.withdraw(
                signer_id,
                [(
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    auth_call.attached_deposit.as_yoctonear(),
                )],
                Some("withdraw"),
                false,
            )?;
```

**File:** contracts/defuse/core/src/engine/state/cached.rs (L362-374)
```rust
    fn auth_call(&mut self, signer_id: &AccountIdRef, auth_call: AuthCall) -> Result<()> {
        if !auth_call.attached_deposit.is_zero() {
            self.internal_sub_balance(
                signer_id,
                [(
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    auth_call.attached_deposit.as_yoctonear(),
                )],
            )?;
        }

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/intents/auth_call.rs (L17-37)
```rust
    #[private]
    pub fn do_auth_call(signer_id: AccountId, auth_call: AuthCall) -> Promise {
        if !auth_call.attached_deposit.is_zero() {
            require!(
                promise_result_checked_void(0).is_ok(),
                "near_withdraw failed",
            );
        }

        let min_gas = auth_call.min_gas();
        let mut p = Promise::new(auth_call.contract_id);

        if let Some(state_init) = auth_call.state_init {
            p = p.state_init(state_init, NearToken::ZERO);
        }

        ext_auth_callee::ext_on(p)
            .with_attached_deposit(auth_call.attached_deposit)
            .with_static_gas(min_gas)
            .on_auth(signer_id, auth_call.msg)
    }
```
