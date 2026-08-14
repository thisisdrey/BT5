### Title
MEV-Extractable Value Theft via Front-Run/Back-Run of `lending_pool_emissions_deposit` — ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
`lending_pool_emissions_deposit` is a fully permissionless instruction that lumps a caller-supplied token amount into a bank's `liquidity_vault` and immediately re-prices `asset_share_value` for *all* existing depositors pro-rata to their current share of `total_asset_shares`. Because there is no time-lock, vesting, or minimum holding period on deposits/withdrawals in marginfi, this is structurally the same "lump-sum, pro-rata `distribute()`" pattern flagged in the external report: any actor who can transiently inflate their own share count immediately before the distribution call, and shrink it back immediately after, captures a disproportionate cut of the distributed amount at the expense of genuine long-term depositors.

### Finding Description
`lending_pool_emissions_deposit` computes the new `asset_share_value` as `(total_assets + amount) / total_asset_shares`, where `total_asset_shares` is read at call time [1](#0-0) . This means every unit of `total_asset_shares` present *at the moment the instruction executes* receives an equal cut of `amount`, regardless of how long that depositor has actually held shares.

The instruction is explicitly documented and implemented as permissionless — anyone can be the `depositor` funding the deposit — and the only checks are for zero amount, paused bank state, and problematic mint extensions [2](#0-1) . Meanwhile, `lending_account_deposit` and `lending_account_withdraw` have no cooldown, vesting, or minimum-holding-period logic — a user can `accrue_interest`, mint shares via `deposit`, and later `withdraw` in the very next instruction/transaction with `time_delta` for interest accrual being effectively zero [3](#0-2) [4](#0-3) .

This directly mirrors the external report's "Case 2" (attacker with control over transaction ordering): the attacker does not need a flash loan at all — they only need the ability to place a deposit transaction immediately before the `lending_pool_emissions_deposit` call and a withdraw transaction immediately after (e.g., via a Jito bundle or by simply racing/tipping to control the block's transaction ordering). Since the deposit and withdraw are of the same underlying asset with negligible elapsed time, there is no interest-rate or price exposure — it is a risk-free arbitrage against the pro-rata distribution.

### Impact Explanation
This constitutes unauthorized transfer of value: capital that the admin/protocol intended to distribute to genuine, existing lenders of a bank (as compensation, e.g., "same-bank emissions") is partially or substantially redirected to a transient MEV actor who contributed no real liquidity duration. In the worst case (attacker able to deposit a very large sum right before the call, e.g., via a large capital pool or their own flashloan-style borrow-and-deposit within marginfi itself), the attacker could capture the overwhelming majority of the distributed amount, effectively stealing from real depositors.

### Likelihood Explanation
Exploitability depends on the attacker's ability to guarantee transaction ordering around calls to `lending_pool_emissions_deposit`. Since the instruction is permissionless and typically invoked periodically by admins/integrators (per the emissions guides), an MEV searcher monitoring the mempool/leader schedule (e.g. via Jito bundles on Solana) can reliably front-run and back-run it. No privileged access is required by the attacker — only capital and the ability to atomically bundle deposit → (target's emissions deposit) → withdraw. This is a moderate-to-high likelihood MEV pattern given Solana's existing bundle infrastructure.

### Recommendation
- Do not use a single-instruction, instantaneous pro-rata distribution mechanism for emissions/rewards. Instead, accrue rewards over time proportional to a time-weighted share balance (similar to how the rate-based `emissions_outstanding` per-`Balance` mechanism already works elsewhere in the codebase), rather than an instantaneous lump-sum re-pricing of `asset_share_value`.
- If lump-sum distribution must be retained, require the deposited amount to vest into `asset_share_value` gradually over a window (e.g., via `cache.accumulated_since_last_update`-style linear release) rather than an atomic jump, removing the incentive to snipe the exact block of the call.
- Alternatively, gate `lending_pool_emissions_deposit` execution such that it cannot be atomically preceded/followed by deposit/withdraw from the same signer/authority within the same slot or transaction bundle, or require a minimum holding period before newly deposited shares are eligible for a pro-rata cut of same-block emissions deposits.

### Proof of Concept
1. Admin/integrator prepares to call `lending_pool_emissions_deposit(amount)` on Bank B, which will raise `asset_share_value` for all current holders of `total_asset_shares`.
2. Attacker (a searcher with Jito-bundle-level ordering control, or simply someone monitoring for the emissions-deposit transaction) submits a bundle:
   a. `lending_account_deposit` — attacker deposits `X` (their own capital or capital borrowed via a marginfi flashloan against another bank) into Bank B immediately before the admin's transaction, minting shares `s = X / asset_share_value_before`.
   b. The admin's `lending_pool_emissions_deposit(amount)` executes, computing `new_share_value = (total_assets + X + amount) / (total_shares + s)` [5](#0-4) .
   c. Attacker immediately calls `lending_account_withdraw` (`withdraw_all`) redeeming their shares `s` at `new_share_value`, capturing `s * new_share_value - X ≈ amount * s / (total_shares + s)`.
3. Because steps (a)–(c) occur within the same slot/bundle, `time_delta` for interest accrual is ~0 [6](#0-5) , so the attacker bears no interest cost and no price risk — the extracted amount is pure profit taken from what would otherwise have been distributed to genuine depositors.

**Uncertainty**: I was unable to fully confirm (due to tool-call limits) whether there are additional guards elsewhere in the codebase (e.g., in `BankAccountWrapper::deposit`/`withdraw` in `programs/marginfi/src/state/marginfi_account.rs`, which I located but did not have time to read in full) that might impose minimum holding periods, per-slot deposit/withdraw restrictions, or other mitigations not visible in the snippets reviewed. I also could not verify whether `SECURITY.md` explicitly lists this MEV/sandwich pattern as a known, accepted, out-of-scope issue. A Devin session with full repository access would be needed to rule these out definitively.

### Citations

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

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L69-93)
```rust
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

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L98-131)
```rust
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

**File:** programs/marginfi/src/state/bank.rs (L520-523)
```rust
        let time_delta: u64 = (current_timestamp - self.last_update).try_into().unwrap();
        if time_delta == 0 {
            return Ok(());
        }
```
