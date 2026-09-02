## Answer



### Title
Defuse credits deposits based on the declared `ft_on_transfer` amount without verifying the token balance actually received, breaking the TokenId-vs-asset-moved binding for fee-on-transfer/deflationary NEP-141 tokens - (File: contracts/defuse/src/contract/tokens/nep141/deposit.rs)

### Summary
`Contract::ft_on_transfer` credits the depositor's internal ledger with the `amount` parameter supplied in the `ft_transfer_call` callback, with no reconciliation against the actual `ft_balance_of(defuse)` delta on the underlying token contract. For any NEP-141 token that applies a transfer fee/deflationary mechanism and reports the pre-fee `amount` in its `ft_on_transfer` call (the exact bug class from the Balancer/STA report), Defuse mints more internal MT balance for that `TokenId` than the real tokens it actually custodies.

### Finding Description
`ft_on_transfer` unconditionally calls `self.deposit(receiver_id, [(token_id, amount.0)], ...)` using the caller-supplied `amount`, never checking the delta in real token balance held by the contract: [1](#0-0) 

`Contract::deposit` then simply adds this trusted `amount` to `total_supplies` and to the receiver's `token_balances` for that `TokenId`: [2](#0-1) 

There is no whitelist restricting which NEP-141 contracts may be deposited (`grep` for whitelist/registered-token logic in `contracts/defuse/**` returns nothing), and no call to `ft_balance_of` to reconcile the credited amount against the actual balance change on the token contract. The internal `TokenId::Nep141(token_contract_id)` is meant to represent 1:1 custody of the real token held by the Defuse contract account, but the credited supply is driven entirely by what the (possibly fee-on-transfer) token contract chooses to report as `amount`, not by what Defuse's real balance increased by.

This breaks the binding "the asset a `TokenId` names versus the asset moved": `total_supplies[token_id]` (and the sum of all depositors' `token_balances[token_id]`) can exceed the real quantity of the underlying fungible token actually held in Defuse's account.

### Impact Explanation
If any depositor uses a deflationary/fee-on-transfer NEP-141 token that reports the pre-fee `amount` to `ft_on_transfer` (mirroring STA's behavior on Balancer), the attacker's own account is credited more internal balance for that `TokenId` than tokens actually delivered to Defuse. When this inflated balance is later moved out via `ft_withdraw`/`FtWithdraw` intent — which subtracts from `token_balances`/`total_supplies` and issues an outbound `ft_transfer` for the full ledger amount: [3](#0-2) 
— any other honest depositor of the same token contract who deposited real value into the shared pool ends up with insufficient real underlying tokens backing their own credited balance, since Defuse's true `ft_balance_of` for that token contract was never enough to cover the inflated total. This is Critical impact per the rubric: value credited without matching real asset movement, ultimately producing unrecoverable losses/frozen funds for other legitimate depositors of that token.

### Likelihood Explanation
Exploitation requires only that an attacker deposit (or induce another depositor to deposit) a fee-on-transfer/deflationary NEP-141 token whose `ft_on_transfer` reports the originally-requested amount rather than the delivered amount — a well-documented, common token behavior (the very class the Balancer/STA report describes), requiring no privileged role, no relayer key, and no DAO/upgrade action. Any unprivileged account can trigger the deposit path.

### Recommendation
Reconcile deposits against the real balance delta: capture `ft_balance_of(env::current_account_id())` on the token contract before initiating/at deposit-callback time (or require the token to be from a trusted/whitelisted registry known not to apply transfer fees), and credit the account with `min(declared_amount, actual_balance_increase)` rather than blindly trusting the caller-supplied `amount` in `ft_on_transfer`.

### Proof of Concept
1. Deploy (or use) a NEP-141 token `FEE_FT` that charges a transfer fee (e.g., 10%) but calls back `ft_on_transfer(sender_id, amount=U128(requested_amount), msg)` using the pre-fee `requested_amount` rather than the amount actually credited to Defuse's balance — this mirrors STA's real-world implementation referenced in the report.
2. Honest user A deposits 1,000 `FEE_FT` via `ft_transfer_call` into Defuse; Defuse's real `FEE_FT` balance increases by only 900, but `ft_on_transfer` reports `amount=1000`, so `Contract::deposit` credits A with 1000 `TokenId::Nep141(FEE_FT)` — see [4](#0-3) .
3. Attacker repeats step 2 to inflate cumulative internal `total_supplies` for that `TokenId` beyond Defuse's actual `FEE_FT` holdings.
4. Attacker calls `ft_withdraw` for their full inflated internal balance — see [5](#0-4)  — draining Defuse's real `FEE_FT` reserve.
5. Honest user A's subsequent withdrawal of their legitimately-credited balance fails or is only partially fulfilled because Defuse's actual token reserve was already exhausted by the attacker, demonstrating the ledger no longer matches real custody.

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

**File:** contracts/defuse/src/contract/tokens/mod.rs (L17-65)
```rust
impl Contract {
    pub(crate) fn deposit(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
        memo: Option<&str>,
    ) -> Result<()> {
        let owner = self
            .storage
            .accounts
            .get_or_create(owner_id.clone())
            // deposits are allowed for locked accounts
            .as_inner_unchecked_mut();

        let mut mint_event = MtMintEvent {
            owner_id: owner_id.into(),
            token_ids: Vec::new().into(),
            amounts: Vec::new().into(),
            memo: memo.map(Into::into),
        };

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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L27-50)
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
