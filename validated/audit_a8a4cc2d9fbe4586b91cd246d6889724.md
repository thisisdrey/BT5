## Analysis

The reported bug class (dividend-sniping via sandwiching a visible reward-distribution transaction with a deposit+withdrawal) maps directly onto marginfi's permissionless `lending_pool_emissions_deposit` instruction, which raises `asset_share_value` for all existing depositors pro-rata without minting new shares for the incoming value. [1](#0-0) 

Key mechanics: this instruction transfers `amount` from an arbitrary `emissions_funding_account` into the bank's `liquidity_vault`, then recomputes `asset_share_value = updated_total_assets / total_asset_shares`, where `total_asset_shares` is whatever the pool had **at the moment the deposit lands** [2](#0-1) . Any address holding shares at that instant benefits proportionally, regardless of how long it has held them, exactly the pattern the GG-5 report describes for `distributeDividends`.

Unlike the report's mitigating assumption (that capturing the "dividend" requires swapping for the token and providing/removing liquidity, making it gas/slippage prohibitive), here an attacker only needs to:
1. Call plain `lending_account_deposit` on the same-mint bank right before the pending `lending_pool_emissions_deposit` transaction lands [3](#0-2) .
2. Let the emissions deposit inflate `asset_share_value`.
3. Call `lending_account_withdraw` (or `withdraw_all`) immediately after [4](#0-3) .

No swap, no LP provisioning, and no price-impact/slippage cost is required — only two plain token transfers of the bank's own mint — so the Bridges Team's original "cost prohibitive" reasoning does not carry over to this code path. There is no warmup period, share lock, or minimum holding time gating deposits/withdrawals in this flow.

That said, this requires the attacker to observe (in the mempool, or via predictable/scheduled campaign funding patterns) that a `lending_pool_emissions_deposit` transaction is about to land, and the profit is bounded by `attacker_shares / (attacker_shares + pre-existing_shares) * emissions_amount`, so profitability scales with how much capital the attacker can flash into the pool relative to existing depositors, and is only economically meaningful for pools with large emissions deposits relative to TVL.

### Title
Emissions-deposit dividend sniping via deposit/withdraw sandwich - (File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs)

### Summary
`lending_pool_emissions_deposit` is a permissionless instruction that raises a bank's `asset_share_value` for all current shareholders pro-rata, without any warmup or lock period. An attacker can front-run a pending emissions deposit with `lending_account_deposit` and immediately follow it with `lending_account_withdraw`/`withdraw_all`, capturing a slice of the reward intended for genuine long-term depositors while only briefly holding the position.

### Finding Description
`lending_pool_emissions_deposit` transfers funder tokens into the liquidity vault and recomputes `bank.asset_share_value` as `updated_total_assets / total_asset_shares` using whatever `total_asset_shares` exists at execution time [5](#0-4) . Because deposits and withdrawals of the underlying mint are unrestricted and immediate (`lending_account_deposit` / `lending_account_withdraw`, both with no cooldown) [6](#0-5) [7](#0-6) , an attacker who deposits just before the emissions transaction lands and withdraws just after captures a proportional share of the injected value without contributing to the campaign or bearing meaningful holding-period risk.

### Impact Explanation
This dilutes the reward genuine long-term lenders were meant to receive from an emissions/incentive campaign, transferring value from legitimate depositors to a sniping bot. It is an unauthorized redistribution of value within the accounting system, matching the reported bug class (dividend sniping), and directly analogous to the `distributeDividends` sandwich in the external report.

### Likelihood Explanation
Feasibility is higher here than in the original report because no swap or liquidity provisioning is needed — just two plain SPL transfers into/out of the bank's own mint, so the cost-prohibitive argument the Bridges Team gave for the original bug does not apply to this instruction. The attack does require observing the emissions-deposit transaction before it lands (mempool visibility or predictable campaign cadence) and sufficient capital relative to existing TVL to make the captured slice worthwhile net of two transaction fees.

### Recommendation
Introduce a minimum holding period (warmup) before a deposit becomes eligible to receive same-block/same-slot emissions increases, or mint emissions as a separate accrual bucket that is only distributed pro-rata to shares that existed before the emissions-deposit instruction began, rather than instantaneously repricing `asset_share_value` for all current shares.

### Proof of Concept
1. Bank X has `total_asset_shares = S` from legitimate depositors, `asset_share_value = V`.
2. Attacker observes a pending `lending_pool_emissions_deposit(amount)` transaction for bank X.
3. Attacker submits `lending_account_deposit(D)` just before it lands, minting `D/V` shares, so `total_asset_shares` becomes `S + D/V`.
4. Emissions deposit executes: `asset_share_value` becomes `(S*V + D + amount)/(S + D/V)`.
5. Attacker calls `lending_account_withdraw(withdraw_all=true)` immediately, redeeming at the new inflated share value, netting a profit equal to `(D/V)/(S+D/V) * amount` minus two transaction fees, while legitimate depositors' realized gain from `amount` is correspondingly reduced.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L84-146)
```rust
/// Permissionlessly deposit same-mint emissions directly into the bank liquidity vault,
/// increasing depositor value through asset share value.
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

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L33-93)
```rust
pub fn lending_account_deposit<'info>(
    mut ctx: Context<'info, LendingAccountDeposit<'info>>,
    amount: u64,
    deposit_up_to_limit: Option<bool>,
) -> MarginfiResult {
    let LendingAccountDeposit {
        marginfi_account: marginfi_account_loader,
        authority: signer,
        signer_token_account,
        liquidity_vault: bank_liquidity_vault,
        token_program,
        bank: bank_loader,
        group: marginfi_group_loader,
        ..
    } = ctx.accounts;
    let clock = Clock::get()?;
    let maybe_bank_mint = utils::maybe_take_bank_mint(
        &mut ctx.remaining_accounts,
        &*bank_loader.load()?,
        token_program.key,
    )?;
    let deposit_up_to_limit = deposit_up_to_limit.unwrap_or(false);

    let mut bank = bank_loader.load_mut()?;
    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    let group = marginfi_group_loader.load()?;
    validate_asset_tags(&bank, &marginfi_account)?;
    validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;

    check!(
        !marginfi_account.get_flag(ACCOUNT_DISABLED)
            // Sanity check: liquidation doesn't allow the deposit ix, but just in case
            && !marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP),
        MarginfiError::AccountDisabled
    );

    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        bank_loader.key(),
    )?;

    let deposit_amount = if deposit_up_to_limit {
        amount.min(bank.get_remaining_deposit_capacity()?)
    } else {
        amount
    };

    if deposit_amount == 0 {
        return Ok(());
    }

    let mut bank_account = BankAccountWrapper::find_or_create(
        &bank_loader.key(),
        &mut bank,
        &mut marginfi_account.lending_account,
    )?;

    let share_amount = bank_account.deposit(I80F48::from_num(deposit_amount))?;
    marginfi_account.last_update = clock.unix_timestamp as u64;
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L45-131)
```rust
pub fn lending_account_withdraw<'info>(
    mut ctx: Context<'info, LendingAccountWithdraw<'info>>,
    amount: u64,
    withdraw_all: Option<bool>,
) -> MarginfiResult {
    let LendingAccountWithdraw {
        marginfi_account: marginfi_account_loader,
        destination_token_account,
        liquidity_vault: bank_liquidity_vault,
        token_program,
        bank_liquidity_vault_authority,
        bank: bank_loader,
        group: marginfi_group_loader,
        ..
    } = ctx.accounts;
    let clock = Clock::get()?;

    let withdraw_all = withdraw_all.unwrap_or(false);
    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    let group = marginfi_group_loader.load()?;

    {
        let maybe_bank_mint = {
            let bank = bank_loader.load()?;
            utils::maybe_take_bank_mint(&mut ctx.remaining_accounts, &bank, token_program.key)?
        };

        let in_receivership_or_order_execution =
            marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION);
        let mut bank = bank_loader.load_mut()?;
        validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;

        // Fetch oracle price for rate limiting and deleverage tracking
        // When group rate limiter is enabled, oracle is required
        let group_rate_limit_enabled = group.rate_limiter.is_enabled();
        let price = if in_receivership_or_order_execution || group_rate_limit_enabled {
            let price = fetch_asset_price_for_bank_low_bias(
                &bank_loader.key(),
                &bank,
                &clock,
                ctx.remaining_accounts,
            )?;

            // Validate price is non-zero during liquidation/deleverage to prevent exploits
            if in_receivership_or_order_execution {
                check!(price > I80F48::ZERO, MarginfiError::ZeroAssetPrice);
            }

            price
        } else {
            I80F48::ZERO
        };

        bank.accrue_interest(
            clock.unix_timestamp,
            &group,
            #[cfg(not(feature = "client"))]
            bank_loader.key(),
        )?;

        let liquidity_vault_authority_bump = bank.liquidity_vault_authority_bump;

        let in_receivership = marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP);
        let lending_account = &mut marginfi_account.lending_account;
        let mut bank_account =
            BankAccountWrapper::find(&bank_loader.key(), &mut bank, lending_account)?;

        let (amount_pre_fee, share_amount) = if withdraw_all {
            // Note: In liquidation, we still want this passed on the books
            bank_account.withdraw_all(in_receivership)?
        } else {
            let amount_pre_fee = maybe_bank_mint
                .as_ref()
                .map(|mint| {
                    utils::calculate_pre_fee_spl_deposit_amount(
                        mint.to_account_info(),
                        amount,
                        clock.epoch,
                    )
                })
                .transpose()?
                .unwrap_or(amount);

            let share_amount = bank_account.withdraw(I80F48::from_num(amount_pre_fee))?;

            (amount_pre_fee, share_amount)
        };
```
