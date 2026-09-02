**No vulnerability found for this question.**

The scoped file `contracts/wallet/src/state.rs` does not contain any of the cited symbols (`AuthCall`, `min_gas`, `DO_AUTH_CALL_MIN_GAS`, `STATE_INIT_GAS`) — it only defines the wallet's `State<PubKey>` struct (signature flag, nonces, extensions, public key) [1](#0-0) . The actual `AuthCall`/`min_gas` logic lives in `contracts/defuse/core/src/intents/auth.rs` [2](#0-1)  and `contracts/defuse/src/contract/intents/auth_call.rs` (`DO_AUTH_CALL_MIN_GAS`, `STATE_INIT_GAS`, `do_auth_call`) [3](#0-2) , an entirely different contract (`defuse`, the Verifier) from the wallet contract named in the question.

Beyond the file mismatch, tracing the actual `do_auth_call` path: `Contract::auth_call` in `contracts/defuse/src/contract/intents/state.rs` schedules `do_auth_call` and detaches it without any callback that inspects the resolved status of the `on_auth` promise [4](#0-3) . There is no `internal_add_balance`/`internal_sub_balance` or resolver credit gated on `on_auth`'s promise success anywhere in this call path — `do_auth_call` itself just returns a `Promise` and succeeds as soon as it schedules the nested call, which is standard NEAR promise semantics, not a broken authorization binding [5](#0-4) . Since no balance-affecting logic in this repository is keyed off `do_auth_call`'s or `on_auth`'s promise outcome, the claimed impact ("resolver logic keyed off do_auth_call's promise success ... if such logic exists in extensions") is explicitly speculative and not demonstrated in the current codebase, failing the requirement for exact file/fn support and a reproducible fund-movement proof.

### Citations

**File:** contracts/wallet/src/state.rs (L24-41)
```rust
pub struct State<PubKey> {
    /// Whether authentication by signature is allowed.
    pub signature_enabled: bool,

    /// Subwallet id: enables a single public key to have multiple different
    /// wallet-contracts.
    pub subwallet_id: u32,

    /// Public key of the signer (depends on the signature schema being
    /// being used by the implementation)
    pub public_key: PubKey,

    /// Set of used timeout-based nonces.
    pub nonces: Nonces,

    /// A set of enabled extensions.
    pub extensions: BTreeSet<AccountId>,
}
```

**File:** contracts/defuse/core/src/intents/auth.rs (L44-51)
```rust
impl AuthCall {
    pub const MIN_GAS_DEFAULT: Gas = Gas::from_tgas(10);

    #[inline]
    pub fn min_gas(&self) -> Gas {
        self.min_gas.unwrap_or(Self::MIN_GAS_DEFAULT)
    }
}
```

**File:** contracts/defuse/src/contract/intents/auth_call.rs (L10-37)
```rust
    pub(crate) const DO_AUTH_CALL_MIN_GAS: Gas = Gas::from_tgas(5);

    /// Covers `StateInit` (NEP-616) cost when deterministic account doesn't exist yet.
    /// Only accounts for deploying via Global Contract ref (NEP-591) with <770B storage
    /// which doesn't require storage staking.
    pub const STATE_INIT_GAS: Gas = Gas::from_tgas(15);

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

**File:** contracts/defuse/src/contract/intents/state.rs (L303-337)
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

            ext_wnear::ext(self.wnear_id.clone())
                .with_attached_deposit(NearToken::from_yoctonear(1))
                .with_static_gas(NEAR_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .near_withdraw(U128(auth_call.attached_deposit.as_yoctonear()))
                .then(
                    // do_auth_call only after unwrapping NEAR
                    Self::ext(env::current_account_id())
                        .with_static_gas(
                            Self::auth_call_callback_gas(&auth_call)
                                .ok_or(DefuseError::GasOverflow)?,
                        )
                        .do_auth_call(signer_id.to_owned(), auth_call),
                )
        }
        .detach();

        Ok(())
    }
```
