This confirms the finding: the doc comment itself explicitly acknowledges the behavior — `contracts/defuse/core/src/intents/auth.rs` states `NOTE: the wNEAR will not be refunded in case of fail.` [1](#0-0) 

The flow is: `State::auth_call` calls `self.withdraw` to debit the signer's wNEAR (`internal_sub_balance`), unwraps it via `near_withdraw`, then chains `.then(...do_auth_call(...))` with no further `.then(...)` resolver at all. [2](#0-1)  `do_auth_call` then forwards the unwrapped NEAR as `attached_deposit` to `on_auth` on the attacker-controlled `contract_id`, and this is the terminal promise in the chain — there is no subsequent resolver callback in the contract to catch a failed `on_auth` and re-credit the signer. [3](#0-2) 

This is unlike `nft_resolve_withdraw`, `ft_resolve_withdraw`, or `mt_resolve_withdraw`, which chain a `#[private]` resolver after the token-transfer promise to inspect `PromiseResult` and refund on failure. [4](#0-3)  For `AuthCall`, once wNEAR is unwrapped to NEAR and attached to `on_auth`, if the callee's `on_auth` panics or otherwise fails, the attached NEAR is returned to the Verifier contract account as an unused-deposit refund by the NEAR protocol (since a failed FunctionCall action refunds any attached deposit back to the predecessor, i.e., the Verifier contract), but the Verifier contract has no code path that detects this and credits it back to the signer's balance — the deposit is simply absorbed into the contract's own NEAR balance while the signer's wNEAR balance was already permanently decremented.

### Title
Permanent loss of signer's `attached_deposit` wNEAR on failed `on_auth` in `AuthCall` (no resolver/refund) - ([File: contracts/defuse/src/contract/intents/state.rs])

### Summary
`State::auth_call` debits the signer's wNEAR balance for `attached_deposit`, unwraps it to NEAR, and attaches it to a call to an attacker-controlled `contract_id::on_auth`, with no resolver callback anywhere in the promise chain to detect failure and re-credit the signer. An attacker who controls `contract_id` (which they choose in their own signed intent, or which a victim signs but points at an attacker contract) can make `on_auth` fail cheaply, causing the debited wNEAR to be permanently lost from the signer's perspective while the unused NEAR deposit is silently absorbed by the Verifier contract account instead of returning to the intents accounting.

### Finding Description
The broken binding: `wNEAR debited from signer for attached_deposit` should equal `wNEAR re-credited to signer on on_auth failure + NEAR delivered to contract_id on success`. Tracing `State::auth_call` in `contracts/defuse/src/contract/intents/state.rs` lines 303-337: when `auth_call.attached_deposit` is non-zero, it calls `self.withdraw(signer_id, [(wnear_token, attached_deposit)], ...)` which runs `internal_sub_balance` on the signer immediately and synchronously (in-state, no promise needed for this step), then schedules `ext_wnear::near_withdraw(...)` followed by `.then(Self::ext(...).do_auth_call(signer_id, auth_call))`. `do_auth_call` in `contracts/defuse/src/contract/intents/auth_call.rs` asserts `near_withdraw` succeeded, then creates a `Promise` to `auth_call.contract_id` (optionally with `state_init`) and calls `on_auth(signer_id, msg)` with `attached_deposit` — this is the *final* promise in the chain; there is no further `.then(...)` to inspect the `on_auth` result. The doc comment in `contracts/defuse/core/src/intents/auth.rs` line 30 explicitly says "the wNEAR will not be refunded in case of fail." An attacker deploys a contract implementing `AuthCallee::on_auth` that immediately calls `env::panic_str` (or otherwise fails cheaply) regardless of `msg`, then signs (or gets a victim to sign) an intent containing `AuthCall { contract_id: <attacker_contract>, attached_deposit: X, .. }`. Once executed, `internal_sub_balance` for `X` wNEAR already happened before the promise chain runs; when `on_auth` panics, the attached NEAR deposit is refunded by NEAR protocol back to the Verifier contract's own account balance (as predecessor), not to the signer's intents balance, and no code observes this event to re-credit the signer.

### Impact Explanation
Every execution of `AuthCall` with non-zero `attached_deposit` against a contract whose `on_auth` fails destroys `attached_deposit` worth of wNEAR from the signer's balance with zero recovery path — the funds are not returned to the signer nor properly accounted for by the Verifier, resulting in permanently frozen/lost user funds. This is repeatable per signer per `AuthCall` intent (any account, any amount up to their wNEAR balance) and matches the Critical category "user funds permanently frozen." However, note that in the ordinary/expected use, the signer themselves chooses `contract_id` and `msg` in their own signed intent (this is a self-authorizing action, similar to `ft_transfer_call`), so the primary attacker-controllable scenario is a user signing an `AuthCall` toward a malicious `contract_id` (whether that's an intentional self-inflicted call to a scam contract, or a relayer/dApp tricking a user into signing such an intent) — it is not an attacker draining an unrelated victim's balance without the victim's own signature over that specific `AuthCall`.

### Likelihood Explanation
The precondition is that some account with a wNEAR balance in the Verifier signs (or is tricked into signing) an `AuthCall` intent with non-zero `attached_deposit` targeting a contract that has (or can be made to have) a failing `on_auth`. The attacker's cost is only deploying a trivial `on_auth` implementation that panics; no special privileges, relayer keys, or roles are needed. This is fully attacker-controlled and reproducible whenever `AuthCall` is used with `attached_deposit`, since the code path and doc comment already document that no refund occurs on failure.

### Recommendation
Add a `#[private]` `auth_call_resolve` callback chained after `do_auth_call`'s `on_auth` promise that inspects `PromiseResult` and calls `self.deposit(signer_id, [(wnear_token, attached_deposit)], Some(REFUND_MEMO))` on failure, mirroring the pattern used in `nft_resolve_withdraw` / `ft_resolve_withdraw` / `mt_resolve_withdraw`.

### Proof of Concept
Using `near-workspaces` sandbox (non-mainnet):
1. Deploy the Defuse Verifier contract and a minimal `on_auth` receiver contract whose `on_auth` implementation calls `env::panic_str("fail")` unconditionally.
2. Fund a signer account with wNEAR inside the Verifier (deposit + storage_deposit as needed) — record `balance_before = signer's wnear balance in Verifier`.
3. Sign and execute (`execute_intents`) an `AuthCall` intent: `contract_id = failing_receiver`, `attached_deposit = X`, `msg = "anything"`.
4. Await promise resolution; assert the `on_auth` receipt failed (panicked).
5. Query `mt_balance_of` / equivalent for signer's wNEAR `TokenId` in the Verifier: assert `balance_after == balance_before - X` (i.e., permanently reduced), demonstrating `balance_before - balance_after = X ≠ 0` with no corresponding credit anywhere, violating the equality `debited == re-credited_on_failure + delivered_on_success`.

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

**File:** contracts/defuse/src/contract/tokens/nep171/withdraw.rs (L159-195)
```rust
#[near]
impl NonFungibleTokenWithdrawResolver for Contract {
    #[private]
    fn nft_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        token_id: non_fungible_token::TokenId,
        is_call: bool,
    ) -> bool {
        let used = if is_call {
            // `nft_transfer_call` returns true if token was successfully transferred
            match promise_result_checked_json::<bool>(0) {
                Ok(Ok(used)) => used,
                Ok(Err(_deserialization_err)) => false,
                // do not refund on failed `nft_transfer_call` due to
                // NEP-141 vulnerability: `nft_resolve_transfer` fails to
                // read result of `nft_on_transfer` due to insufficient gas
                Err(_) => true,
            }
        } else {
            // `nft_transfer` returns empty result on success
            promise_result_checked_void(0).is_ok()
        };

        if !used {
            self.deposit(
                sender_id,
                [(Nep171TokenId::new(token, token_id).into(), 1)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        used
    }
}
```
