No vulnerability found for this question.

The premise doesn't hold under close inspection of the actual code paths:

1. **`has_public_key` fallback is correct by design, not exploitable.** The fallback `account_id == public_key.to_implicit_account_id()` in [1](#0-0)  reflects NEAR's own implicit-account semantics: an implicit `AccountId` *is* derived from, and always implicitly owned by, its corresponding public key. This lets someone sign intents as `signer_id = <their own implicit account>` before that account has ever been "created" in `self.accounts`. The scenario the question describes (attacker owns the private key matching `victim_account_id`) is explicitly just the attacker acting as themselves under an implicit account id — not an authorization bypass on any real victim.

2. **`commit_nonce` creating a fresh `AccountEntry` does not create spendable balance.** `commit_nonce` in `State for Contract` at [2](#0-1)  uses `get_or_create`, but this only initializes nonce/public-key bookkeping — it does not touch `token_balances`.

3. **Withdrawals cannot draw down a zero/non-existent balance.** The contract-level `internal_sub_balance` (used by `ft_withdraw`/`nft_withdraw`/`mt_withdraw`/`native_withdraw`) requires an *existing* account via `get_mut`, and fails with `DefuseError::AccountNotFound` if no entry exists at all: [3](#0-2) . Even if an entry exists (e.g. via `commit_nonce`'s `get_or_create`), the actual subtraction goes through `Amounts::sub`, which uses `checked_sub` and returns `None`/`BalanceOverflow` on underflow: [4](#0-3) . A zero balance can never be pushed negative or "pass validation."

4. **Deposits always create the `AccountEntry` first.** `internal_add_balance` also uses `get_or_create`: [5](#0-4) . So there is no reachable state where an account has real, persisted funds but no `AccountEntry` — the two are created together.

5. **No true "racing" exists in the claimed sense.** NEAR contract calls execute receipts sequentially to completion; there is no shared-mutable-state race between two `execute_intents` calls "in the same block" analogous to a thread race. Additionally, `commit_nonce`/`is_nonce_used` (via `MaybeLegacyNonces::commit`) reject any nonce reuse before intent execution proceeds: [6](#0-5) , so the same signed payload cannot be replayed to double the effect described.

The binding "signer_id the engine authorises == the account whose balance actually changes" holds: `internal_sub_balance`/`internal_add_balance` always operate on the exact `signer_id`/`owner_id` passed by the engine, guarded by `checked_sub`/`checked_add`, and no code path lets a withdrawal succeed against an unfunded or non-existent balance.

### Citations

**File:** contracts/defuse/src/contract/intents/state.rs (L42-50)
```rust
    fn has_public_key(&self, account_id: &AccountIdRef, public_key: &PublicKey) -> bool {
        self.accounts
            .get(account_id)
            .map(Lock::as_inner_unchecked)
            .map_or_else(
                || account_id == public_key.to_implicit_account_id(),
                |account| account.has_public_key(account_id, public_key),
            )
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L123-130)
```rust
    #[inline]
    fn commit_nonce(&mut self, account_id: AccountId, nonce: Nonce) -> Result<()> {
        self.accounts
            .get_or_create(account_id.clone())
            .get_mut()
            .ok_or(DefuseError::AccountLocked(account_id))?
            .commit_nonce(nonce)
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L147-169)
```rust
    fn internal_add_balance(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_or_create(owner_id)
            // we allow locked accounts to accept deposits and incoming deposits
            .as_inner_unchecked_mut();

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }
            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        Ok(())
    }
```

**File:** contracts/defuse/src/contract/intents/state.rs (L171-195)
```rust
    fn internal_sub_balance(
        &mut self,
        owner_id: &AccountIdRef,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
    ) -> Result<()> {
        let owner = self
            .accounts
            .get_mut(owner_id)
            .ok_or_else(|| DefuseError::AccountNotFound(owner_id.to_owned()))?
            .get_mut()
            .ok_or_else(|| DefuseError::AccountLocked(owner_id.to_owned()))?;

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            owner
                .token_balances
                .sub(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }

        Ok(())
    }
```

**File:** contracts/defuse/core/src/amounts.rs (L77-82)
```rust
    pub fn sub(&mut self, k: T::K, amount: u128) -> Option<T::V>
    where
        T::V: CheckedSub<u128>,
    {
        self.checked_apply(k, |a| a.checked_sub(amount))
    }
```

**File:** contracts/defuse/src/contract/accounts/account/nonces.rs (L45-58)
```rust
    #[inline]
    pub fn commit(&mut self, nonce: Nonce) -> Result<()> {
        // Check legacy maps for used nonce
        if self
            .legacy
            .as_ref()
            .is_some_and(|legacy| legacy.is_used(nonce))
        {
            return Err(DefuseError::NonceUsed);
        }

        // New nonces can be committed only to the new map
        self.nonces.commit(nonce)
    }
```
