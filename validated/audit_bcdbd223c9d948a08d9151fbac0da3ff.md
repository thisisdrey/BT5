### Title
`AuthCall` withdraws user's internal wNEAR balance and sends it via a fire-and-forget `on_auth` promise with no failure resolver, permanently freezing user funds if the call fails - (File: `contracts/defuse/src/contract/intents/auth_call.rs`, `contracts/defuse/src/contract/intents/state.rs`)

### Summary
This is a NEAR-native analog of the reported bug class: an external/cross-contract call is made without any mechanism to detect and react to its failure, so the caller's internal accounting is left inconsistent with what was actually delivered. Instead of a Solidity `call()` implicitly returning `true` for a non-existent destination, here a NEAR `Promise` to an arbitrary (attacker/user-chosen) `contract_id::on_auth()` is dispatched with an attached NEAR deposit and **no `.then()` resolver and no `refund_to`**, so a failed receipt's refund is captured by the `defuse` contract's own account balance instead of being credited back to the signer's internal ledger.

### Finding Description
`Contract::auth_call` debits the signer's internal wNEAR balance and unwraps it into real NEAR via `near_withdraw`, then chains a callback to `do_auth_call`: [1](#0-0) 

`do_auth_call` creates the terminal promise that sends the withdrawn NEAR deposit to `auth_call.contract_id::on_auth(...)`. Crucially, this promise is returned directly with no `.then()` callback and no `refund_to()` override: [2](#0-1) 

`NearPromise` (used elsewhere in the wallet contracts) explicitly documents this NEAR semantic: failed/unused deposits are refunded "by default... to the caller's account itself" unless `refund_to` is set: [3](#0-2) 

Other places in the codebase where the contract sends NEAR/tokens out and must handle failure explicitly use dedicated `*_resolve_*` callbacks that check `promise_result_checked_*` and re-credit the sender on failure, e.g. `ft_resolve_withdraw`, `nft_resolve_withdraw`, `mt_resolve_withdraw`: [4](#0-3) 

`do_auth_call` has no equivalent resolver. If the final `on_auth` FunctionCall fails (e.g. `contract_id` does not exist, was deleted between signing and relaying, lacks the `on_auth` method, or panics/runs out of gas), the attached NEAR deposit is refunded by the NEAR runtime to the `defuse` contract's own account balance (the implicit "caller"), not to the signer. Meanwhile the signer's internal wNEAR balance was already permanently debited in `auth_call`'s `self.withdraw(...)` call, with no code path to restore it.

### Impact Explanation
This breaks the binding "value debited versus value delivered plus refunded": the signer's internal ledger is debited by `attached_deposit`, but on failure neither is the value delivered to `contract_id` nor is it credited back to the signer — it silently becomes untracked NEAR sitting in the `defuse` contract's own balance, unreachable by any user-facing method. This matches the accepted Critical impact category of "funds permanently frozen."

### Likelihood Explanation
Any signer that includes `AuthCall` with a non-zero `attached_deposit` in a signed intent is exposed whenever `contract_id` fails to execute `on_auth` successfully at execution time (deleted/undeployed account, incompatible/panicking callee, or insufficient `min_gas`/`min_gas` mis-estimation by the client). Since intents can be relayed by a permissionless relayer at a time later than signing, the destination account's existence/behavior is not guaranteed at execution time, mirroring exactly the "Alice submits a call to an address believed to be a contract... the contract has been destroyed" scenario from the analog report.

### Recommendation
- **Short term:** Add a `.then()` resolver to the final `on_auth` promise in `do_auth_call` that checks the promise result via `promise_result_checked_void`/`promise_result_checked_json`, and re-credits the signer's internal wNEAR balance (via `self.deposit(...)`, mirroring the pattern in `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw`) when the call fails.
- **Long term:** Audit all fire-and-forget `Promise`/`NearPromise` usages that carry value out of internal ledgers (e.g. `auth_call`, `storage_deposit`) to ensure every value-bearing outgoing call has either an explicit resolver or a `refund_to` targeting the signer, so failures cannot silently strand funds in the contract's own balance.

### Proof of Concept
1. User deposits and holds internal wNEAR balance of `N` yoctoNEAR in the `defuse` contract.
2. User signs a `MultiPayload` containing an `AuthCall { contract_id: <account with no on_auth method / soon-to-be-deleted account>, attached_deposit: N, msg: "...", min_gas: None }`.
3. A relayer submits this payload to `execute_intents`, invoking `Contract::auth_call` → `self.withdraw(signer_id, [(wnear_token, N)], ...)`, debiting the signer's internal ledger by `N`.
4. `near_withdraw` succeeds, unwrapping `N` NEAR held by the `defuse` contract; `.then(do_auth_call(...))` fires.
5. `do_auth_call` creates the terminal `Promise::new(contract_id)...on_auth(...)` with attached deposit `N` and no resolver.
6. Because `contract_id` doesn't implement/exist for `on_auth`, the receipt fails; NEAR runtime refunds `N` back to the `defuse` contract's own account balance (default `refund_to`), not to the signer.
7. Signer's internal ledger remains debited by `N` with no compensating credit — `N` is now permanently frozen inside the `defuse` contract's untracked balance.

### Citations

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

**File:** crates/near/promise/src/lib.rs (L24-34)
```rust
pub struct NearPromise {
    /// Receiver of the receipt to be created.
    pub receiver_id: AccountId,

    /// Receiver for refunds of failed or unused NEAR deposits.
    /// By default, it's the caller's account itself.
    #[cfg_attr(
        feature = "serde",
        serde(default, skip_serializing_if = "Option::is_none")
    )]
    pub refund_to: Option<AccountId>,
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-181)
```rust
#[near]
impl FungibleTokenWithdrawResolver for Contract {
    #[private]
    fn ft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        amount: U128,
        is_call: bool,
    ) -> U128 {
        let used = if is_call {
            // `ft_transfer_call` returns successfully transferred amount
            match promise_result_checked_json::<U128>(0) {
                Ok(Ok(used)) => used.0.min(amount.0),
                Ok(Err(_deserialize_err)) => 0,
                // do not refund on failed `ft_transfer_call` due to
                // NEP-141 vulnerability: `ft_resolve_transfer` fails to
                // read result of `ft_on_transfer` due to insufficient gas
                Err(_) => amount.0,
            }
        } else {
            // `ft_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amount.0
            } else {
                0
            }
        };
```
