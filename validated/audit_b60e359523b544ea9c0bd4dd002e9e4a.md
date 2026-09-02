### Title
Defuse deposit accounting trusts `ft_on_transfer`'s `amount` parameter without verifying actual NEP-141 balance received, enabling insolvency with fee-on-transfer/tax tokens - (File: contracts/defuse/src/contract/tokens/nep141/deposit.rs)

### Summary
`ft_on_transfer` credits the depositor's internal virtual balance with the `amount` argument as reported by the caller/token contract, without ever comparing it against the actual change in defuse's own NEP-141 token balance. If any deposited token applies a fee/tax on transfer (reducing the amount actually received by the receiving account below the nominal `amount` argument), defuse's internal ledger (`token_balances`) will be over-credited relative to the tokens it actually holds for that `TokenId`.

### Finding Description
`ft_on_transfer` receives `amount: U128` from the caller and immediately calls `self.deposit(receiver_id, [(token_id, amount.0)], ...)`: [1](#0-0) 

`deposit()` then unconditionally adds `amount` to the owner's `token_balances` and to `total_supplies` for that `TokenId`, with no balance-based verification against the underlying FT contract: [2](#0-1) 

The equality that should hold is: `Δ(defuse's real NEP-141 balance for token)` == `Δ(sum of internal virtual token_balances credited for that TokenId)`. This is broken whenever the deposited token contract does not deliver the full `amount` to defuse (e.g. a deflationary/tax/fee-on-transfer NEP-141 token, or any token whose `ft_transfer_call` reports a different value to the receiver than what it actually retains for the receiver's balance). Since defuse accepts deposits for arbitrary NEP-141 token contracts (the `TokenId::Nep141` is derived from `predecessor_account_id()`, i.e. any token contract can call in), nothing in the withdraw/deposit code path checks defuse's own FT balance before/after the transfer.

This mirrors the OpenLevV1 finding's root cause: the amount "indicated" by the counterpart (there, the DEX swap result; here, the caller-provided `amount` in the deposit callback) is trusted as-is for internal accounting, instead of being reconciled against actually-received value.

Once the internal ledger for that `TokenId` is over-credited, withdrawals reduce `token_balances` by the requested amount and issue an `ft_transfer`/`ft_transfer_call` for that amount via `internal_ft_withdraw` / `do_ft_withdraw`: [3](#0-2) 

Because the pool of that specific token, as actually held by defuse, is smaller than the aggregate internal virtual balances of depositors, later withdrawers of that same token by other (honest) depositors will find the FT contract's actual balance insufficient — their `ft_transfer` calls will fail on-chain (insufficient balance in the FT contract), or, if partial fills are possible depending on token semantics, they receive less than their internal balance implied. This is a shared-pool insolvency: the attacker's over-credited deposit is redeemable up to the point where the discrepancy manifests, and other depositors of the same token absorb the shortfall (frozen/lost funds), exactly as in the OpenLev report where "actual funds received can be less than accounted... draining contract funds... or... freezing of user's funds."

### Impact Explanation
This crosses the "value debited versus value delivered plus refunded" boundary in the ruleset: the internal ledger credits a value not actually delivered to defuse's real token balance. The impact is Critical-adjacent: for any deposited NEP-141 token with even minimal transfer-fee/tax behavior, an attacker can systematically inflate their internal balance relative to the pool's real holdings for that token, then withdraw the excess, permanently depleting the shared pool for that specific `TokenId` and causing other legitimate holders of the same token to be unable to fully withdraw (funds frozen/lost).

### Likelihood Explanation
Likelihood depends on whether any deposited NEP-141 token implements non-1:1 transfer semantics (fee-on-transfer/tax/rebasing). Defuse's `ft_on_transfer` is generic and accepts any `predecessor_account_id()` as a valid token source, i.e., it is not restricted to trusted/whitelisted 1:1 tokens at the protocol level shown here, so any unprivileged user (depositor) choosing to deposit such a token can trigger the mismatch without needing any special role or victim key.

### Recommendation
Do not trust the caller-supplied `amount` for internal crediting. Where feasible, reconcile the credited amount against the actual NEP-141 balance delta of the contract (e.g., query/track `ft_balance_of(current_account_id)` for the specific token before and after the transfer, similar to a balance-before/after check), or restrict deposits to an explicitly allow-listed set of tokens known to be strictly 1:1 conforming, so fee-on-transfer/tax tokens cannot be deposited at all.

### Proof of Concept
1. A NEP-141 token `TAX` is deployed such that `ft_transfer_call(receiver=defuse, amount=1000, msg=...)` decreases sender's balance by 1000 but only increases defuse's real `TAX` balance by 950 (5% tax), while still invoking `defuse::ft_on_transfer(sender, amount=U128(1000), msg)` as per near-contract-standards behavior for tax-token variants.
2. `ft_on_transfer` calls `self.deposit(receiver_id, [(TAX_token_id, 1000)])`, crediting the receiver's internal `token_balances[TAX]` by 1000, per [4](#0-3) . Defuse's actual `TAX` balance only increased by 950.
3. Attacker withdraws their full internal balance of 1000 `TAX` via `FtWithdraw`, per [3](#0-2) , which is honored as long as the FT contract holds ≥1000 (e.g., aggregated with other depositors' real balances for the same token).
4. Repeating deposits/withdrawals of `TAX` (or simply having several depositors of `TAX`) drains the actual `TAX` reserve faster than the internal ledger reflects, until other holders of `TAX` in defuse can no longer withdraw their full internal balance — their `ft_transfer` promise fails against insufficient real balance, freezing/losing their funds.

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

**File:** contracts/defuse/src/contract/tokens/mod.rs (L18-65)
```rust
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

**File:** contracts/defuse/src/contract/tokens/nep141/withdraw.rs (L53-106)
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

        let is_call = withdraw.is_call();
        Ok(if let Some(storage_deposit) = withdraw.storage_deposit {
            ext_wnear::ext(self.wnear_id.clone())
                .with_attached_deposit(NearToken::from_yoctonear(1))
                .with_static_gas(NEAR_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .near_withdraw(U128(storage_deposit.as_yoctonear()))
                .then(
                    // schedule storage_deposit() only after near_withdraw() returns
                    Self::ext(env::current_account_id())
                        .with_static_gas(
                            Self::DO_FT_WITHDRAW_GAS
                                .checked_add(withdraw.min_gas())
                                .ok_or(DefuseError::GasOverflow)
                                .unwrap_or_else(|err| err.panic()),
                        )
                        .do_ft_withdraw(withdraw.clone()),
                )
        } else {
            Self::do_ft_withdraw(withdraw.clone())
        }
        .then(
            Self::ext(env::current_account_id())
                .with_static_gas(Self::FT_RESOLVE_WITHDRAW_GAS)
                // do not distribute remaining gas here
                .with_unused_gas_weight(0)
                .ft_resolve_withdraw(withdraw.token, owner_id, withdraw.amount.into(), is_call),
        )
        .into())
    }
```
