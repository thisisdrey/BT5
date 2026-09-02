### Title
Deposit crediting trusts the reported `amount` instead of verifying actual balance received, breaking `value debited == value delivered` for fee-on-transfer NEP-141/NEP-245 tokens - (`File: contracts/defuse/src/contract/tokens/nep141/deposit.rs`)

### Summary
`Contract::ft_on_transfer` (the NEP-141 deposit entrypoint of the intents contract) credits the internal ledger with exactly the `amount` value reported by the calling token contract, without ever comparing it to the token balance the intents contract actually received. Any token contract (permissionlessly deployable and depositable — there is no whitelist check on `env::predecessor_account_id()`) that implements a fee-on-transfer style accounting internally can report a gross `amount` to `ft_on_transfer` while only crediting the intents contract's real token balance with a smaller net amount. The intents contract will mint full credit for the gross amount, over-crediting its internal ledger relative to the real tokens it holds for that `TokenId`.

### Finding Description
`ft_on_transfer` in `contracts/defuse/src/contract/tokens/nep141/deposit.rs` derives the `TokenId` from `env::predecessor_account_id()` and unconditionally calls `self.deposit(...)` with the caller-supplied `amount.0`: [1](#0-0) 

`Contract::deposit` (`contracts/defuse/src/contract/tokens/mod.rs`) then unconditionally adds `amount` to `total_supplies` and to the receiver's `token_balances`, with no verification against the actual NEP-141 balance the contract holds for that token: [2](#0-1) 

The same pattern exists for multi-token deposits in `mt_on_transfer`: [3](#0-2) 

By contrast, the **withdraw** path explicitly does *not* trust a reported amount: it inspects the actual promise result of `ft_transfer_call`/`ft_transfer` to determine how much was really sent, and only refunds the difference: [4](#0-3) 

This asymmetry is the root cause: outbound transfers are reconciled against reality, but inbound deposits are not. A NEP-141 token contract is free to implement its own internal fee-on-transfer logic — nothing in the NEP-141 interface requires that the `amount` argument passed to `ft_on_transfer` equal the actual balance increase recorded by that token for the intents contract's account. Since depositing arbitrary NEP-141/NEP-245 tokens into `defuse` requires no permission (any `predecessor_account_id` is accepted as a valid `TokenId`), any token integrated into the protocol that charges a transfer fee (or any token contract that simply misreports `amount`) causes the intents contract's internal ledger (`total_supplies` / per-account `token_balances`) to be permanently inflated relative to the real token reserve backing that `TokenId`.

This breaks the conservation invariant that should hold for every `TokenId`:
```
sum(internal credited balances for TokenId) == actual token balance held by defuse contract for TokenId
```
Once inflated, later legitimate withdrawals of that token by other holders can fail (their internally-credited balance can't be redeemed because the real underlying balance was never fully delivered), or an attacker can be the first to redeem the phantom excess balance before other holders, extracting value that was never actually deposited.

### Impact Explanation
This crosses the explicitly listed "value debited versus value delivered plus refunded" custody boundary. The intents contract's ledger becomes under-collateralized for the affected `TokenId`, which is a fund-freezing / value-loss condition for legitimate holders of that token inside the protocol, and lets whoever triggers/exploits the discrepancy redeem more real tokens than were actually delivered to the contract (at the expense of other depositors of the same `TokenId`), i.e., tokens credited/withdrawn without a valid backing balance. This matches the Critical impact bucket ("tokens moved, credited or withdrawn without valid authorisation ... or funds permanently frozen").

### Likelihood Explanation
Depositing arbitrary NEP-141 tokens is fully permissionless in `defuse` (no admin whitelist gate on `ft_on_transfer`'s `predecessor_account_id`), so any unprivileged actor can integrate/exploit a token whose contract reports a gross `amount` to `ft_on_transfer` while only delivering a net amount to `defuse`'s real balance. No relayer, DAO role, or upgrade is needed — a normal `ft_transfer_call` from any token contract into `defuse` triggers the vulnerable code path.

### Recommendation
In `ft_on_transfer`/`mt_on_transfer`, compare the token contract's real balance for the intents contract before and after crediting (or, for NEP-141, cap the credited amount to the token's own before/after `ft_balance_of(current_account_id())` delta) rather than trusting the `amount` parameter blindly, mirroring the reconciliation already done on the withdraw path in `ft_resolve_withdraw`.

### Proof of Concept
1. Deploy a custom NEP-141 token contract `FEE_TOKEN` whose `ft_transfer_call` implementation debits the sender the full `amount`, but only credits `defuse`'s balance with `amount * 90%` (a 10% transfer fee), while still invoking `ft_on_transfer(sender_id, amount /* gross */, msg)`.
2. Attacker calls `FEE_TOKEN.ft_transfer_call(defuse, 1000, ..., msg)`. `defuse` actually receives 900 `FEE_TOKEN` in its account, but `ft_on_transfer` (`contracts/defuse/src/contract/tokens/nep141/deposit.rs:38-43`) credits the attacker's internal `Nep141TokenId(FEE_TOKEN)` balance with the full `1000`.
3. Attacker (or any other integrator who later deposits the same token honestly) now has more credited `FEE_TOKEN` balance in `defuse` than the contract actually holds.
4. Attacker calls `ft_withdraw` for `1000` `FEE_TOKEN`. Because the internal ledger says `1000` is owed, `defuse` attempts `ft_transfer`/`ft_transfer_call` of `1000` real `FEE_TOKEN`, but only `900` actually exist in `defuse`'s account — either this withdrawal drains reserves meant for other depositors of `FEE_TOKEN` (letting the attacker walk away with `100` tokens that were never really deposited by them), or it fails, permanently locking out honest depositors of the same token whose credited balance can no longer be fully redeemed.

### Citations

**File:** contracts/defuse/src/contract/tokens/nep141/deposit.rs (L19-43)
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
```

**File:** contracts/defuse/src/contract/tokens/mod.rs (L38-65)
```rust
        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            mint_event.token_ids.to_mut().push(token_id.to_string());
            mint_event.amounts.to_mut().push(amount);

            let total_supply = self
                .storage
                .state
                .total_supplies
                .add(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            match token_id {
                TokenId::Nep171(ref tid) => {
                    if total_supply > 1 {
                        return Err(DefuseError::NftAlreadyDeposited(tid.clone()));
                    }
                }
                TokenId::Nep141(_) | TokenId::Nep245(_) | TokenId::Imt(_) => {}
            }

            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }
```

**File:** contracts/defuse/src/contract/tokens/nep245/deposit.rs (L19-73)
```rust
    fn mt_on_transfer(
        &mut self,
        sender_id: AccountId,
        previous_owner_ids: Vec<AccountId>,
        token_ids: Vec<defuse_nep245::TokenId>,
        amounts: Vec<U128>,
        msg: String,
    ) -> PromiseOrValue<Vec<U128>> {
        let token = env::predecessor_account_id();

        require!(!amounts.is_empty(), "invalid args");

        require!(
            token_ids.len() == amounts.len(),
            "NEP-245: Contract MUST panic if `token_ids` length does not equals `amounts` length"
        );

        require!(
            previous_owner_ids.len() == token_ids.len(),
            "NEP-245: Contract MUST panic if `previous_owner_ids` length does not equals `token_ids` length"
        );

        require!(
            token != env::current_account_id(),
            "self-wrapping is not allowed"
        );

        let core_token_ids = token_ids
            .iter()
            .inspect(|token_id| {
                if token_id.len() > MAX_TOKEN_ID_LEN {
                    DefuseError::TokenIdTooLarge(token_id.len()).panic();
                }
            })
            .cloned()
            .map(|token_id| Nep245TokenId::new(token.clone(), token_id))
            .map(Into::into);

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
            core_token_ids
                .clone()
                .zip(amounts.iter().map(|amount| amount.0)),
            Some("deposit"),
        )
        .unwrap_or_else(|err| err.panic());
```

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L164-181)
```rust
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
