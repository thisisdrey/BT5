Confirmed: `ft_withdraw` has no `owner_id` parameter, and `ensure_auth_predecessor_id` returns `env::predecessor_account_id()` directly, which is passed as `owner_id` into `internal_ft_withdraw` and then into `self.withdraw(&owner_id, ...)`. The binding `owner_id == env::predecessor_account_id()` holds structurally — there is no attacker-controlled parameter that could substitute a different `owner_id`. [1](#0-0) [2](#0-1) 

The scenario described (attacker "pre-positions" as an AccountId that later becomes the real owner's Verifier account) isn't a vulnerability in this code path: `owner_id` in `internal_ft_withdraw` is *always* exactly `env::predecessor_account_id()`, sourced from `ensure_auth_predecessor_id()` with no override possible — the function signature has no `owner_id` field at all. What the question calls "pre-positioning as predecessor" is really just: whoever controls the NEAR account with a given `AccountId` string *is* that account's predecessor when calling directly, by definition of how NEAR accounts and predecessor IDs work. This isn't a flaw in the Defuse contract; it is the intended trust model for `auth_by_predecessor_id` — the design explicitly trusts the NEAR account owner of `owner_id` (i.e., whoever holds the private key controlling that NEAR account) to authenticate via direct calls, as an alternative to signature-based `PublicKey` auth. There is no code path where `env::predecessor_account_id() != owner_id` reaches `internal_ft_withdraw`.

The "attacker creates a namesake account before the real owner registers" framing describes a general NEAR account-squatting concern (whoever controls an `AccountId` on NEAR controls what that `AccountId` does as predecessor), not a bug in `ensure_auth_predecessor_id` or `ft_withdraw`. If someone else's application later treats deposits/interactions under that same `AccountId` as belonging to a "real owner" who never actually controlled the NEAR account, that's an assumption failure outside this contract's control — the contract only ever credits/debits balances to the account that itself signs the transaction or is the predecessor, never a third party. No path exists in `ft_withdraw`/`internal_ft_withdraw`/`ensure_auth_predecessor_id` allowing `predecessor_account_id != owner_id`. [3](#0-2) [4](#0-3) 

#No vulnerability found for this question.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L27-51)
```rust
    fn ft_withdraw(
        &mut self,
        token: AccountId,
        receiver_id: AccountId,
        amount: U128,
        memo: Option<String>,
        msg: Option<String>,
    ) -> PromiseOrValue<U128> {
        assert_one_yocto();
        self.internal_ft_withdraw(
            self.ensure_auth_predecessor_id(),
            FtWithdraw {
                token,
                receiver_id,
                amount: amount.into(),
                memo,
                msg,
                storage_deposit: None,
                min_gas: None,
            },
            false,
        )
        .unwrap_or_else(|err| err.panic())
    }
}
```

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

**File:** contracts/defuse/src/contract/accounts/mod.rs (L78-85)
```rust
    #[inline]
    pub fn ensure_auth_predecessor_id(&self) -> AccountId {
        let predecessor_account_id = env::predecessor_account_id();
        if !StateView::is_auth_by_predecessor_id_enabled(self, &predecessor_account_id) {
            DefuseError::AuthByPredecessorIdDisabled(predecessor_account_id).panic();
        }
        predecessor_account_id
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L87-93)
```rust
    #[inline]
    fn is_auth_by_predecessor_id_enabled(&self, account_id: &AccountIdRef) -> bool {
        self.accounts
            .get(account_id)
            .map(Lock::as_inner_unchecked)
            .is_none_or(Account::is_auth_by_predecessor_id_enabled)
    }
```
