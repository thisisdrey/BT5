### Title
`native_withdraw` debits a user's internal NEAR balance without any resolver to refund on a failed native transfer - (File: `contracts/defuse/src/contract/intents/state.rs`)

### Summary
Every other withdrawal path in the Defuse contract (`ft_withdraw`, `nft_withdraw`, `mt_withdraw`) subtracts the user's internal balance and then schedules a `*_resolve_withdraw` callback that inspects the transfer's promise result and re-credits (refunds) the user if the outbound transfer failed. The `native_withdraw` path decrements the user's internal wNEAR-denominated balance the same way, but the final native `Promise::transfer()` it schedules has no resolver at all, so a failed transfer is never refunded to the user.

### Finding Description
`native_withdraw` in `contracts/defuse/src/contract/intents/state.rs` subtracts the withdrawal amount from the owner's internal ledger via `self.withdraw(...)`, then calls `wnear.near_withdraw()` followed by `do_native_withdraw`, and finally `.detach()`s the promise chain — no callback ever inspects the outcome: [1](#0-0) 

`do_native_withdraw` in `contracts/defuse/src/contract/tokens/nep141/native.rs` only checks that `near_withdraw` succeeded, then performs a raw native transfer as the terminal action of the promise chain, with nothing observing whether it succeeds: [2](#0-1) 

Contrast this with the `ft_withdraw`/`nft_withdraw`/`mt_withdraw` flows, which all chain a `*_resolve_withdraw` callback that checks `promise_result_checked_void`/`promise_result_checked_json` and calls `self.deposit(...)` to refund the sender if the transfer did not fully succeed: [3](#0-2) [4](#0-3) [5](#0-4) 

A native `Promise::transfer()` action can fail (e.g. `receiver_id` is a non-existent named account, or the destination receipt runs out of gas/errors in a way that fails the receipt), and unlike an ERC20-style call there is no return value to check — but the codebase's own established pattern for every other asset type is to always add a resolver that verifies success and refunds the internal ledger on failure. `native_withdraw` breaks this custody binding: `balance debited == value delivered + value refunded` no longer holds, because the refund side of the equation is missing entirely for the native/wNEAR withdrawal path.

### Impact Explanation
If the outbound `transfer` action fails after the user's internal wNEAR balance has already been decremented and the underlying wNEAR has already been unwrapped via `near_withdraw`, the user's internal ledger permanently loses the withdrawn amount with no compensating refund intent ever created — this is a direct, unauthorized/uncompensated loss of user funds inside the settlement engine, matching the "funds permanently frozen"/lost-value class of Critical impact defined for this scan.

### Likelihood Explanation
This is reachable by any account holder simply calling the standard `NativeWithdraw` intent with a `receiver_id` that causes the terminal `Promise::transfer()` to fail (most straightforwardly, a syntactically valid but non-existent named NEAR account, which is entirely attacker/user-controlled input) — no privileged role, relayer key, or victim key is required, and the surrounding withdraw paths (ft/nft/mt) demonstrate the developers are aware such failures are possible and normally compensate for them.

### Recommendation
Add a `resolve_native_withdraw` callback analogous to `ft_resolve_withdraw`/`nft_resolve_withdraw`/`mt_resolve_withdraw` that runs after the terminal `Promise::transfer()`, checks `promise_result_checked_void`, and calls `self.deposit(...)` with a `REFUND_MEMO` to restore the user's wNEAR-denominated internal balance (and correspondingly re-wrap NEAR back into wNEAR, or otherwise reconcile the contract's raw NEAR back into the deposit ledger) whenever the transfer did not succeed.

### Proof of Concept
1. User deposits NEAR/wNEAR into Defuse and holds an internal wNEAR balance of `X`.
2. User signs and submits a `NativeWithdraw` intent with `amount = X` and `receiver_id` set to a syntactically valid but non-existent named account (e.g. `"nonexistent-account-1234.near"`).
3. `native_withdraw` (`contracts/defuse/src/contract/intents/state.rs:212-240`) subtracts `X` from the user's internal balance, unwraps `X` wNEAR into raw NEAR via `near_withdraw`, then calls `do_native_withdraw`.
4. `do_native_withdraw` (`contracts/defuse/src/contract/tokens/nep141/native.rs:11-19`) issues `Promise::new(receiver_id).transfer(amount)`; since `receiver_id` does not exist, this action fails at the protocol level.
5. Because no resolver observes this failure, the user's internal ledger remains at `-X` with no refund intent ever issued — the `X` NEAR is lost to the user (either refunded silently to the contract's own account balance at the protocol level, or otherwise unaccounted for in the Defuse ledger), demonstrating the broken debit/refund equality.

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L212-240)
```rust
    fn native_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: NativeWithdraw) -> Result<()> {
        self.withdraw(
            owner_id,
            [(
                Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                withdraw.amount.as_yoctonear(),
            )],
            Some("withdraw"),
            false,
        )?;

        ext_wnear::ext(self.wnear_id.clone())
            .with_attached_deposit(NearToken::from_yoctonear(1))
            .with_static_gas(NEAR_WITHDRAW_GAS)
            // do not distribute remaining gas here
            .with_unused_gas_weight(0)
            .near_withdraw(U128(withdraw.amount.as_yoctonear()))
            .then(
                // do_native_withdraw only after unwrapping NEAR
                Self::ext(env::current_account_id())
                    .with_static_gas(Self::DO_NATIVE_WITHDRAW_GAS)
                    // do not distribute remaining gas here
                    .with_unused_gas_weight(0)
                    .do_native_withdraw(withdraw),
            )
            .detach();

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/native.rs (L1-19)
```rust
use defuse_core::intents::tokens::NativeWithdraw;
use defuse_near_utils::promise_result_checked_void;
use near_sdk::{Gas, Promise, near, require};

use crate::contract::{Contract, ContractExt};

#[near]
impl Contract {
    pub(crate) const DO_NATIVE_WITHDRAW_GAS: Gas = Gas::from_tgas(12);

    #[private]
    pub fn do_native_withdraw(withdraw: NativeWithdraw) -> Promise {
        require!(
            promise_result_checked_void(0).is_ok(),
            "near_withdraw failed",
        );

        Promise::new(withdraw.receiver_id).transfer(withdraw.amount)
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L154-194)
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

        let refund = amount.0.saturating_sub(used);
        if refund > 0 {
            self.deposit(
                sender_id,
                [(Nep141TokenId::new(token).into(), refund)],
                Some(REFUND_MEMO),
            )
            .unwrap_or_else(|err| err.panic());
        }

        U128(used)
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

**File:** contracts/defuse/src/contract/tokens/nep245/withdraw.rs (L200-256)
```rust
#[near]
impl MultiTokenWithdrawResolver for Contract {
    #[private]
    fn mt_resolve_withdraw(
        &mut self,
        token: AccountId,
        sender_id: AccountId,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        is_call: bool,
    ) -> Vec<U128> {
        require!(
            token_ids.len() == amounts.len() && !amounts.is_empty(),
            "invalid args"
        );

        let mut used = if is_call {
            // `mt_batch_transfer_call` returns successfully transferred amounts
            match promise_result_checked_json_with_len::<Vec<U128>>(0, amounts.len()) {
                Ok(Ok(used)) if used.len() == amounts.len() => used,
                Ok(_) => vec![U128(0); amounts.len()],
                // do not refund on failed `mt_batch_transfer_call` due to
                // NEP-141 vulnerability: `mt_resolve_transfer` fails to
                // read result of `mt_on_transfer` due to insufficient gas
                Err(_) => amounts.clone(),
            }
        } else {
            // `mt_batch_transfer` returns empty result on success
            if promise_result_checked_void(0).is_ok() {
                amounts.clone()
            } else {
                vec![U128(0); amounts.len()]
            }
        };

        self.deposit(
            sender_id,
            token_ids
                .into_iter()
                .zip(amounts)
                .zip(&mut used)
                .filter_map(|((token_id, amount), used)| {
                    // update min during iteration
                    used.0 = used.0.min(amount.0);
                    let refund = amount.0.saturating_sub(used.0);
                    if refund > 0 {
                        Some((Nep245TokenId::new(token.clone(), token_id).into(), refund))
                    } else {
                        None
                    }
                }),
            Some(REFUND_MEMO),
        )
        .unwrap_or_else(|err| err.panic());

        used
    }
```
