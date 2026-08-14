Found a valid analog. The `lending_pool_emissions_deposit` instruction is a genuine unprivileged path that increases a bank's total deposited value without ever consulting `deposit_limit`, which is exactly the same bug class as the Hifi report (`HToken.depositUnderlying()` minting past the debt ceiling by never calling into the debt-ceiling check).

### Title
Permissionless `lending_pool_emissions_deposit` allows the bank deposit cap to be silently exceeded - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`Bank.config.deposit_limit` is meant to cap the total value depositors can hold in a bank, and it is enforced in `Bank::change_asset_shares` [1](#0-0)  whenever `total_asset_shares` is increased. However `lending_pool_emissions_deposit` grows total bank value a different way — by directly transferring tokens into the liquidity vault and mutating `bank.asset_share_value` — and never calls `change_asset_shares` or checks `deposit_limit` at all.

### Finding Description
The deposit-limit invariant is implemented as a check inside `Bank::change_asset_shares`, gated on `shares.is_positive() && self.config.is_deposit_limit_active()` [2](#0-1) . Every normal deposit path (`lending_account_deposit`, `kamino_deposit`, etc.) routes through `increase_balance_internal` → `bank.change_asset_shares(...)`, so the cap is respected [3](#0-2) .

`lending_pool_emissions_deposit` is a permissionless instruction ("(permissionless) Deposit same-bank emissions directly into liquidity vault and increase depositors' value via `asset_share_value`") [4](#0-3) . Its implementation transfers `amount` tokens into the bank's liquidity vault and then recomputes `asset_share_value = (total_assets + amount) / total_asset_shares`, entirely bypassing `change_asset_shares`: [5](#0-4) 

Nowhere in this function is `bank.config.deposit_limit` or `is_deposit_limit_active()` consulted. Since `total_assets = total_asset_shares * asset_share_value` is exactly the quantity that `get_remaining_deposit_capacity()` and `change_asset_shares` use to enforce the cap [6](#0-5) , this instruction can push a bank's effective total deposited value arbitrarily far past `deposit_limit`, exactly mirroring the Hifi report where `depositUnderlying()` minted hTokens past the debt ceiling because it never went through the `borrow()` check path.

### Impact Explanation
Deposit caps are a primary risk-management lever admins use to bound protocol exposure to a given asset/bank (e.g., to limit blast radius from a bad oracle, thin liquidity, or a risky LST). Any account holder — this instruction requires only a `depositor` signer, no admin/group check on the caller — can call `lending_pool_emissions_deposit` repeatedly to inflate `asset_share_value` for a targeted bank, silently defeating the admin-configured `deposit_limit` and letting the bank's economic exposure grow unbounded relative to the cap the risk team set. This is an unauthorized state change to a risk-critical protocol parameter's enforcement, achieved with an unprivileged instruction.

### Likelihood Explanation
High. The instruction is explicitly documented and intended to be permissionless/user-callable [4](#0-3) , requires only that `total_asset_shares > 0` (i.e., the bank has at least one depositor) [7](#0-6) , and needs no special privileges, cooperating counterparties, or race conditions to trigger — a single transaction with sufficient token balance suffices.

### Recommendation
Add a deposit-limit check to `lending_pool_emissions_deposit` mirroring `Bank::change_asset_shares`: after computing `updated_total_assets`, if `config.is_deposit_limit_active()`, reject (or clamp) the deposit when `updated_total_assets` would exceed `config.deposit_limit` (scaled per `ASSET_TAG_DRIFT` handling as done elsewhere).

### Proof of Concept
1. Admin creates a bank with `deposit_limit = D` and at least one depositor so `total_asset_shares > 0`.
2. A normal user deposits up to `D` via `lending_account_deposit`; further deposits correctly fail with `BankAssetCapacityExceeded` per existing test coverage [8](#0-7) .
3. That same (or any) user instead calls `lending_pool_emissions_deposit(bank, amount)` with `amount` far exceeding the remaining capacity, using a funding token account they control. The instruction transfers the tokens into the vault and unconditionally raises `bank.asset_share_value` [9](#0-8) .
4. Total bank value (`total_asset_shares * asset_share_value`) now exceeds `deposit_limit`, with no error raised — confirmed by the existing test suite only asserting share-value math, never a cap check, for this instruction [10](#0-9) .

### Citations

**File:** programs/marginfi/src/state/bank.rs (L258-286)
```rust
    fn get_remaining_deposit_capacity(&self) -> MarginfiResult<u64> {
        if !self.config.is_deposit_limit_active() {
            return Ok(u64::MAX);
        }

        let current_assets = self.get_asset_amount(self.total_asset_shares.into())?;

        let limit = if self.config.asset_tag == ASSET_TAG_DRIFT {
            scale_drift_deposit_limit(self.config.deposit_limit, self.mint_decimals)?
        } else {
            I80F48::from_num(self.config.deposit_limit)
        };

        if current_assets >= limit {
            return Ok(0);
        }

        let remaining = limit
            .checked_sub(current_assets)
            .ok_or_else(math_error!())?
            .checked_sub(I80F48::ONE) // Subtract 1 to ensure we stay under limit
            .ok_or_else(math_error!())?
            .checked_floor()
            .ok_or_else(math_error!())?
            .checked_to_num::<u64>()
            .ok_or_else(math_error!())?;

        Ok(remaining)
    }
```

**File:** programs/marginfi/src/state/bank.rs (L288-316)
```rust
    fn change_asset_shares(
        &mut self,
        shares: I80F48,
        bypass_deposit_limit: bool,
    ) -> MarginfiResult {
        let total_asset_shares: I80F48 = self.total_asset_shares.into();
        self.total_asset_shares = total_asset_shares
            .checked_add(shares)
            .ok_or_else(math_error!())?
            .into();

        if shares.is_positive() && self.config.is_deposit_limit_active() && !bypass_deposit_limit {
            let total_deposits_amount = self.get_asset_amount(self.total_asset_shares.into())?;

            // For Drift banks, deposit_limit is in native decimals but total_deposits_amount
            // is in 9-decimal (DRIFT_SCALED_BALANCE_DECIMALS). We Scale deposit_limit to match.
            let deposit_limit = if self.config.asset_tag == ASSET_TAG_DRIFT {
                scale_drift_deposit_limit(self.config.deposit_limit, self.mint_decimals)?
            } else {
                I80F48::from_num(self.config.deposit_limit)
            };

            if total_deposits_amount >= deposit_limit {
                let deposits_num: f64 = total_deposits_amount.to_num();
                let limit_num: f64 = deposit_limit.to_num();
                msg!("deposits: {:?} deposit lim: {:?}", deposits_num, limit_num);
                return err!(MarginfiError::BankAssetCapacityExceeded);
            }
        }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1860)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
        bank.change_asset_shares(
            asset_shares_increase,
            matches!(operation_type, BalanceIncreaseType::BypassDepositLimit),
        )?;
```

**File:** programs/marginfi/src/lib.rs (L209-216)
```rust
    /// (permissionless) Deposit same-bank emissions directly into liquidity vault and increase
    /// depositors' value via `asset_share_value`.
    pub fn lending_pool_emissions_deposit(
        ctx: Context<LendingPoolEmissionsDeposit>,
        amount: u64,
    ) -> MarginfiResult {
        marginfi_group::lending_pool_emissions_deposit(ctx, amount)
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L86-146)
```rust
pub fn lending_pool_emissions_deposit(
    ctx: Context<LendingPoolEmissionsDeposit>,
    amount: u64,
) -> MarginfiResult {
    if amount == 0 {
        return Ok(());
    }

    let clock = Clock::get()?;
    let mut bank = ctx.accounts.bank.load_mut()?;
    let group = ctx.accounts.group.load()?;

    utils::validate_bank_state(&bank, utils::InstructionKind::FailsIfPausedOrReduceState)?;

    // Reject mints with non-zero transfer fees or active transfer hooks.
    let mint_ai = ctx.accounts.mint.to_account_info();
    check!(
        !utils::nonzero_fee(mint_ai.clone(), clock.epoch)?,
        MarginfiError::InvalidTransfer
    );
    check!(
        !utils::has_transfer_hook(mint_ai)?,
        MarginfiError::InvalidTransfer
    );

    let total_asset_shares = I80F48::from(bank.total_asset_shares);
    check!(
        total_asset_shares > I80F48::ZERO,
        MarginfiError::EmissionsUpdateError
    );

    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        ctx.accounts.bank.key(),
    )?;

    transfer_checked(
        CpiContext::new(
            ctx.accounts.token_program.key(),
            TransferChecked {
                from: ctx.accounts.emissions_funding_account.to_account_info(),
                to: ctx.accounts.liquidity_vault.to_account_info(),
                authority: ctx.accounts.depositor.to_account_info(),
                mint: ctx.accounts.mint.to_account_info(),
            },
        ),
        amount,
        ctx.accounts.mint.decimals,
    )?;

    let total_assets = bank.get_asset_amount(total_asset_shares)?;
    let updated_total_assets = total_assets
        .checked_add(I80F48::from_num(amount))
        .ok_or_else(math_error!())?;

    bank.asset_share_value = updated_total_assets
        .checked_div(total_asset_shares)
        .ok_or_else(math_error!())?
        .into();
```

**File:** programs/marginfi/tests/user_actions/deposit.rs (L130-170)
```rust
#[tokio::test]
async fn marginfi_account_deposit_failure_capacity_exceeded(
    deposit_cap: f64,
    deposit_amount_ok: f64,
    deposit_amount_failed: f64,
    bank_mint: BankMint,
) -> anyhow::Result<()> {
    // -------------------------------------------------------------------------
    // Setup
    // -------------------------------------------------------------------------

    let test_f = TestFixture::new(Some(TestSettings::all_banks_payer_not_admin())).await;

    // User

    let user_mfi_account_f = test_f.create_marginfi_account().await;
    let user_wallet_balance = get_max_deposit_amount_pre_fee(deposit_amount_failed);
    let bank_f = test_f.get_bank(&bank_mint);
    let user_token_account = bank_f
        .mint
        .create_token_account_and_mint_to(user_wallet_balance)
        .await;

    // -------------------------------------------------------------------------
    // Test
    // -------------------------------------------------------------------------

    bank_f
        .update_config(
            BankConfigOpt {
                deposit_limit: Some(native!(deposit_cap, bank_f.mint.mint.decimals, f64)),
                ..Default::default()
            },
            None,
        )
        .await?;

    let res = user_mfi_account_f
        .try_bank_deposit(user_token_account.key, bank_f, deposit_amount_failed, None)
        .await;
    assert_custom_error!(res.unwrap_err(), MarginfiError::BankAssetCapacityExceeded);
```

**File:** programs/marginfi/tests/misc/emissions_deposit.rs (L211-286)
```rust
#[tokio::test]
async fn emissions_same_bank_deposit_updates_asset_share_value() -> anyhow::Result<()> {
    let test_f = TestFixture::new(Some(TestSettings::all_banks_payer_not_admin())).await;

    let usdc_bank = test_f.get_bank(&BankMint::Usdc);

    let emissions_funding = test_f.usdc_mint.create_token_account_and_mint_to(50).await;

    let depositor_a = test_f.create_marginfi_account().await;
    let depositor_b = test_f.create_marginfi_account().await;

    let depositor_a_usdc = test_f.usdc_mint.create_token_account_and_mint_to(40).await;
    let depositor_b_usdc = test_f.usdc_mint.create_token_account_and_mint_to(60).await;

    let depositor_a_amount = 40;
    depositor_a
        .try_bank_deposit(
            depositor_a_usdc.key,
            usdc_bank,
            depositor_a_amount as f64,
            None,
        )
        .await?;

    let depositor_b_amount = 60;
    depositor_b
        .try_bank_deposit(
            depositor_b_usdc.key,
            usdc_bank,
            depositor_b_amount as f64,
            None,
        )
        .await?;

    let bank_before = usdc_bank.load().await;
    let shares_before = I80F48::from(bank_before.total_asset_shares);
    let share_value_before = I80F48::from(bank_before.asset_share_value);

    let liquidity_vault_before =
        TokenAccountFixture::fetch(test_f.context.clone(), bank_before.liquidity_vault)
            .await
            .balance()
            .await;

    let emissions_deposit = 50;
    usdc_bank
        .try_emissions_deposit(native!(emissions_deposit, "USDC"), emissions_funding.key)
        .await?;

    let bank_after = usdc_bank.load().await;
    let shares_after = I80F48::from(bank_after.total_asset_shares);
    let share_value_after = I80F48::from(bank_after.asset_share_value);

    let liquidity_vault_after =
        TokenAccountFixture::fetch(test_f.context.clone(), bank_after.liquidity_vault)
            .await
            .balance()
            .await;

    let asset_shares_value_multiplier =
        1.0 + emissions_deposit as f64 / (depositor_a_amount + depositor_b_amount) as f64;

    assert_eq!(shares_after, shares_before);

    // Should be equal, zero liabilities are present
    assert_eq!(
        share_value_before
            .checked_mul(I80F48::from_num(asset_shares_value_multiplier))
            .unwrap(),
        share_value_after
    );
    assert_eq!(
        liquidity_vault_after - liquidity_vault_before,
        native!(emissions_deposit, "USDC")
    );
    assert_eq!(I80F48::from(bank_after.emissions_remaining), I80F48::ZERO);
```
