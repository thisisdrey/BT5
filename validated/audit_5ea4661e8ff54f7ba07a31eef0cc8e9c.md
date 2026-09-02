#No vulnerability found for this question.

**Rationale:**

The claimed exploit conflates deposit-crediting with withdrawal, and neither path exhibits the "credit before final" flaw described.

`ft_withdraw`/`nft_withdraw`/`mt_withdraw` (via `internal_ft_withdraw` in [1](#0-0)  and `State::ft_withdraw`/`nft_withdraw`/`mt_withdraw` in [2](#0-1) ) always call `internal_sub_balance` synchronously **before** any outbound `Promise` to the token contract is scheduled. There is no crediting in the withdraw path at all — only debiting, followed by a `*_resolve_withdraw` callback that re-credits (refunds) the owner only if the outbound transfer promise itself failed, e.g. [3](#0-2) . This is the standard debit-then-refund-on-failure pattern, not a "credit-then-reverse" pattern, so no unbacked balance can arise from this entrypoint. Also, `ensure_auth_predecessor_id()` binds `owner_id` to `env::predecessor_account_id()`, so an attacker using `auth_by_predecessor_id` can only withdraw from their own account, never credit or debit another user.

The only place where a token contract's say-so (`env::predecessor_account_id()`) drives a balance increase is `ft_on_transfer` in [4](#0-3) , where `TokenId::Nep141(Nep141TokenId::new(env::predecessor_account_id()))` ties the credited `TokenId` uniquely to the calling FT contract's own account ID, and the credit (`self.deposit(...)`) happens synchronously, with `ft_on_transfer` always returning `PromiseOrValue::Value(0.into())` (i.e., "all used"), so there is no subsequent refund path for the FT contract to reverse. Even if a malicious FT contract calls `ft_on_transfer` without a genuine matching transfer, the resulting bad `TokenId` is scoped exclusively to that malicious contract's account — it cannot collide with, dilute, or be redeemed against any other legitimate token's balance, so it does not cause cross-token insolvency or let a worthless asset "claim a valuable balance."

`mt_tokens_for_owner`/`mt_tokens` in [5](#0-4)  are pure read-only views over `account.token_balances`; they cannot themselves create, mutate, or "credit" any balance, so they cannot be the mechanism by which an unbacked balance is introduced.

No reachable path exists where an attacker using `auth_by_predecessor_id` through `ft_withdraw`/`nft_withdraw`/`mt_withdraw` causes the Verifier to hold a `token_balances` credit that isn't backed by an asset it actually custodies for that specific `TokenId`.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L53-74)
```rust
impl Contract {
    pub(crate) fn internal_ft_withdraw(
        &mut self,
        owner_id: AccountId,
        withdraw: FtWithdraw,
        force: bool,
    ) -> Result<PromiseOrValue<U128>> {
        self.withdraw(
            &owner_id,
            iter::once((
                Nep141TokenId::new(withdraw.token.clone()).into(),
                withdraw.amount,
            ))
            .chain(withdraw.storage_deposit.map(|amount| {
                (
                    Nep141TokenId::new(self.wnear_id().into_owned()).into(),
                    amount.as_yoctonear(),
                )
            })),
            Some("withdraw"),
            force,
        )?;
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L156-194)
```rust
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

**File:** contracts/defuse/src/contract/intents/state.rs (L197-210)
```rust
    fn ft_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: FtWithdraw) -> Result<()> {
        self.internal_ft_withdraw(owner_id.to_owned(), withdraw, false)
            .map(PromiseOrValue::detach)
    }

    fn nft_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: NftWithdraw) -> Result<()> {
        self.internal_nft_withdraw(owner_id.to_owned(), withdraw, false)
            .map(PromiseOrValue::detach)
    }

    fn mt_withdraw(&mut self, owner_id: &AccountIdRef, withdraw: MtWithdraw) -> Result<()> {
        self.internal_mt_withdraw(owner_id.to_owned(), withdraw, false)
            .map(PromiseOrValue::detach)
    }
```

**File:** contracts/defuse/src/contract/tokens/nep141/deposit.rs (L19-47)
```rust
    fn ft_on_transfer(
        &mut self,
        sender_id: AccountId,
        amount: U128,
        msg: String,
    ) -> PromiseOrValue<U128> {
        require!(amount.0 > 0, "zero amount");

        let token_id = TokenId::Nep141(Nep141TokenId::new(env::predecessor_account_id()));

        let DepositMessage {
            receiver_id,
            action,
        } = if msg.is_empty() {
            DepositMessage::new(sender_id.clone())
        } else {
            msg.parse().unwrap_or_else(|e| panic!("{e}"))
        };

        self.deposit(
            receiver_id.clone(),
            [(token_id.clone(), amount.0)],
            Some("deposit"),
        )
        .unwrap_or_else(|err| err.panic());

        let Some(action) = action else {
            return PromiseOrValue::Value(0.into());
        };
```

**File:** contracts/defuse/src/contract/tokens/nep245/enumeration.rs (L31-62)
```rust
    fn mt_tokens_for_owner(
        &self,
        account_id: AccountId,
        from_index: Option<U128>,
        limit: Option<u32>,
    ) -> Vec<Token> {
        let from_index = from_index.map_or(0, |v| v.0);
        let from_index: usize = from_index.try_into().unwrap();

        let Some(account) = self.accounts.get(&account_id) else {
            return Vec::new();
        };

        let iter = account
            .as_inner_unchecked()
            .state
            .token_balances
            .iter()
            .skip(from_index)
            .map(|(token_id, _amount)| Token {
                token_id: token_id.to_string(),
                owner_id: match TokenIdType::from(token_id) {
                    TokenIdType::Nep171 => Some(account_id.clone()),
                    TokenIdType::Nep141 | TokenIdType::Nep245 | TokenIdType::Imt => None,
                },
            });

        match limit {
            Some(l) => iter.take(l.try_into().unwrap()).collect(),
            None => iter.collect(),
        }
    }
```
