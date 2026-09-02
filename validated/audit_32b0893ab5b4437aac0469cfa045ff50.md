## Answer

**No vulnerability found for this question.**

The premise — that a nested/re-entrant `execute_intents` call triggered from `on_auth` could execute *before* the outer `Engine::finalize`'s Deltas are persisted — is not achievable under NEAR's execution model, and tracing the code confirms it.

**Binding under test:** `sum(token_balances deltas for T across the outer receipt) == sum(deltas the outer signer authorised)`.

**Trace:**

1. `Engine::execute_signed_intents` iterates over the `MultiPayload`s and calls `execute_signed_intent` for each, then calls `self.finalize()` once at the end — all within a single synchronous Rust function call inside one receipt. [1](#0-0) 

2. Balance mutations are **not deferred** to `finalize()`. `Deltas<S>::internal_add_balance` / `internal_sub_balance` immediately forward to the underlying `state.internal_add_balance` / `internal_sub_balance` (which writes to the persistent `AccountState.token_balances` `IterableMap`), while also recording the delta in the in-memory `TransferMatcher` purely for computing `Transfers` (used for event emission, not for the balance write itself). [2](#0-1) [3](#0-2) 

3. `AuthCall::execute_intent` calls `engine.state.auth_call(...)`, which builds a `Promise` and calls `.detach()` on it. [4](#0-3) [5](#0-4) 

`Promise::detach()` only **schedules** a new NEAR receipt; it does not synchronously invoke the callee. Under NEAR's actor/receipt model, a function call's storage writes (including every `internal_add_balance`/`internal_sub_balance` performed earlier in the same call, and `finalize()`'s invariant check) are committed to the trie when the current receipt's execution finishes — strictly before any receipt it scheduled (i.e., the `do_auth_call` → `on_auth` receipt on `attacker_contract`) begins execution. There is no in-process/synchronous call stack that lets `on_auth` run "before" the outer `finalize()` completes; `on_auth` necessarily runs in a subsequent, separate receipt, by which point the outer transfer/nonce state is already durable.

4. Consequently, when `attacker_contract::on_auth` fires and issues a second `execute_intents` call with a second signed `MultiPayload`, it observes storage that already reflects every balance change and nonce commit from the first (outer) `execute_intents` call. `MaybeLegacyNonces::commit` / `commit_nonce` is applied per-signed-payload before that payload's intents execute, and is part of the same already-committed state, so the two payloads settle sequentially, each fully persisted before the next begins — they cannot interleave or race on the same unspent balance. [6](#0-5) 

**Conclusion:** The attacker can indeed sign an `AuthCall` and have its callback re-invoke `execute_intents` with a second payload, but this is ordinary sequential (not concurrent/re-entrant) settlement — each `execute_intents` receipt fully commits its `Deltas`/`Transfers` before the next one (whether nested via `on_auth` or a plain follow-up transaction) can run. The balance-changes-committed-vs-authorised equality holds at every step; no double-spend of the signer's own balance is possible through this path.

### Citations

**File:** contracts/defuse/core/src/engine/mod.rs (L32-40)
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
```

**File:** contracts/defuse/core/src/engine/mod.rs (L42-83)
```rust
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

**File:** contracts/defuse/src/contract/accounts/state.rs (L1-26)
```rust
use borsh::{BorshDeserialize, BorshSerialize};
use defuse_core::{amounts::Amounts, token_id::TokenId};
use near_sdk::{BorshStorageKey, IntoStorageKey, store::IterableMap};

use crate::contract::prefix::NestPrefix;

#[cfg_attr(feature = "abi", derive(::borsh::BorshSchema))]
#[derive(Debug, BorshSerialize, BorshDeserialize)]
pub struct AccountState {
    pub token_balances: Amounts<IterableMap<TokenId, u128>>,
}

impl AccountState {
    pub fn new<S>(prefix: S) -> Self
    where
        S: IntoStorageKey,
    {
        let parent = prefix.into_storage_key();

        Self {
            token_balances: Amounts::new(IterableMap::new(
                parent.as_slice().nest(AccountStatePrefix::TokenBalances),
            )),
        }
    }
}
```

**File:** contracts/defuse/core/src/intents/auth.rs (L53-65)
```rust
impl ExecutableIntent for AuthCall {
    fn execute_intent<S, I>(
        self,
        signer_id: &AccountIdRef,
        engine: &mut Engine<S, I>,
        _intent_hash: [u8; 32],
    ) -> Result<()>
    where
        S: State,
        I: Inspector,
    {
        engine.state.auth_call(signer_id, self)
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
