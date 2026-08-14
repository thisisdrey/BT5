### Title
Permissionless `lending_pool_emissions_deposit` enables a first-depositor share-inflation attack that zeroes out subsequent depositors' shares - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
`marginfi-v2` prices bank deposits with a stored `asset_share_value` that is multiplied by a depositor's `asset_shares` to get their claim (`get_asset_amount`), and divided into a deposit amount to mint new shares (`get_asset_shares`). Unlike an ERC-4626 vault, this share price is *not* derived from the live token balance of the vault, so a bare SPL transfer into the vault cannot move it. However, the protocol exposes a fully permissionless instruction, `lending_pool_emissions_deposit`, that lets *any* signer directly inflate `asset_share_value` by transferring tokens into the liquidity vault without minting any new shares. Combined with being the sole/first depositor of a bank, this reproduces the classic ERC-4626 "inflation attack" pattern described in the external report: an attacker can cheaply become the dominant share-holder, then use the permissionless emissions-deposit instruction to blow up the share price so that any subsequent depositor's minted shares round down to zero, letting the attacker retain (and later withdraw) both their own and the victim's deposited funds.

### Finding Description
The bank's exchange-rate math lives in `get_asset_amount`/`get_asset_shares`: [1](#0-0) 

`get_asset_shares` floors `value / asset_share_value`, so once `asset_share_value` is large relative to a deposit, the minted shares round to `0`.

The permissionless instruction that can push `asset_share_value` arbitrarily high is `lending_pool_emissions_deposit`. It requires only that `total_asset_shares > 0` (i.e., that at least one active depositor/share exists) and lets *any* signer (`depositor: Signer<'info>`, no admin/authority check) transfer any amount of the bank's mint into the liquidity vault, then recomputes `asset_share_value = (total_assets + amount) / total_asset_shares` — i.e. it directly rewrites the share price without minting any shares to compensate: [2](#0-1) 

The account context confirms there is no privileged signer requirement beyond being the token-account owner making the transfer: [3](#0-2) 

This is registered as `(permissionless)` in the program entrypoint: [4](#0-3) 

Attack path:
1. An attacker deposits a small amount into a fresh (or currently-empty) bank via the ordinary `lending_account_deposit` instruction, becoming the (near-)sole holder of `total_asset_shares` (e.g., `1` unit of shares at `asset_share_value = 1`).
2. The attacker calls `lending_pool_emissions_deposit` with a large amount of the bank's mint token (their own funds). Because `total_asset_shares` is tiny, `asset_share_value` jumps enormously — e.g. from `1` to `1 + amount` — while `total_asset_shares` is left completely unchanged (line 143-146 rewrites `asset_share_value` only, no share issuance).
3. A subsequent, unsuspecting depositor calls `lending_account_deposit` with a normal-sized amount. `bank.get_asset_shares(value)` floors to `0` because `asset_share_value` is now huge, so their tokens are transferred into the liquidity vault but they receive `0` (or a negligible number of) `asset_shares` — this mirrors `increase_balance_internal`'s use of `bank.get_asset_shares(asset_amount_increase)` to mint shares: [5](#0-4) 

4. Because `total_asset_shares` never grew, the attacker's original (tiny) share count now represents the *entire* vault balance — their own funds, their "emissions" deposit, and the victim's stolen deposit — which the attacker can withdraw in full via ordinary withdraw, leaving the victim with an active balance worth ~0.

This is the direct on-chain analog of the `StakePet` inflation attack: an attacker acquiring the first/sole "share" position and then artificially inflating the value each share represents via a non-minting fund injection, causing later depositors' shares to round to zero and effectively confiscating their deposit.

### Impact Explanation
This allows outright theft of user deposits from any bank where an attacker can become (or already is) the dominant/sole share-holder — most realistically on newly created banks before organic liquidity accrues, or any bank whose share count is currently very small (e.g., after most depositors have withdrawn). The victim's tokens are transferred into the real liquidity vault but their `asset_shares` mints to zero, so they have no economic claim; the attacker can withdraw the full vault balance (their funds + the emissions injection + the victim's deposit). This satisfies the "concrete theft / unauthorized transfer" bar: real value moves from an unprivileged victim to an attacker with no bank-admin involvement required, purely through two permissionless, unprivileged-user instructions (`lending_account_deposit` and `lending_pool_emissions_deposit`).

### Likelihood Explanation
Likelihood is bank-state dependent but non-trivial: it is highest immediately after a new bank is created (before any deposits) or after a bank's deposits have been fully withdrawn down to a very small residual share count, both of which are realistic windows given `lending_pool_add_bank`/`lending_pool_add_bank_permissionless` create banks with `total_asset_shares = 0` initially and any user can be first to deposit. `lending_pool_emissions_deposit` has minimal preconditions (`total_asset_shares > 0`, bank not paused/reduce-only, no transfer-fee/hook mint) and is explicitly documented as permissionless, so no special privilege or race beyond deposit-then-emit ordering is required.

### Recommendation
- Do not allow `asset_share_value` to be moved by an unprivileged, permissionless instruction that also lacks a corresponding share mint; either restrict `lending_pool_emissions_deposit` to a trusted role, or require it to proportionally distribute value in a way bounded by a maximum per-call multiplier (e.g., cap the allowed increase in `asset_share_value` per call, or require a minimum `total_asset_shares`/`total_assets` floor before allowing the instruction to run at all).
- Enforce a minimum initial deposit / "dead shares" pattern for every bank at creation (mint a small amount of shares to a burn address or to the bank/group itself) so `total_asset_shares` can never be attacker-controlled down to a trivially small number.
- Add a check in the deposit path that rejects (or refunds) deposits that would mint `0` shares, rather than silently accepting the token transfer with no share credit.

### Proof of Concept
1. Admin creates a new bank `B` (or use an existing bank whose `total_asset_shares` has decayed to a very small value after withdrawals).
2. Attacker calls `lending_account_deposit` with `amount = 1` unit, receiving `asset_shares = 1` at `asset_share_value = 1` (see `get_asset_shares`, `programs/marginfi/src/state/bank.rs:249-256`, and the `increase_balance_internal` mint path at `programs/marginfi/src/state/marginfi_account.rs:1855-1860`). Attacker now owns 100% of `total_asset_shares`.
3. Attacker calls `lending_pool_emissions_deposit(amount = 1_000_000_000)` (their own tokens) against bank `B`. Per `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs:111-146`, this only checks `total_asset_shares > 0`, transfers the tokens into the liquidity vault, and sets `asset_share_value = (1 + 1_000_000_000) / 1 ≈ 1_000_000_001`. `total_asset_shares` remains `1`.
4. Victim calls `lending_account_deposit(amount = 1_000)` expecting normal shares. `get_asset_shares(1000) = floor(1000 / 1_000_000_001) = 0`. Victim's 1,000 tokens are transferred to the vault but their credited `asset_shares` is `0`.
5. Attacker withdraws their `1` share, redeeming `asset_amount = 1 * 1_000_000_001 ≈ 1_000_000_001`, which now includes the victim's 1,000 tokens that the victim can never recover.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L237-256)
```rust
    fn get_asset_amount(&self, shares: I80F48) -> MarginfiResult<I80F48> {
        Ok(shares
            .checked_mul(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }

    fn get_liability_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        Ok(value
            .checked_div(self.liability_share_value.into())
            .ok_or_else(math_error!())?)
    }

    fn get_asset_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        if self.asset_share_value == I80F48::ZERO.into() {
            return Ok(I80F48::ZERO);
        }
        Ok(value
            .checked_div(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L86-156)
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

    bank.update_bank_cache(&group)?;

    msg!(
        "Deposited {} same-bank emissions into liquidity vault",
        amount
    );

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L158-192)
```rust
#[derive(Accounts)]
pub struct LendingPoolEmissionsDeposit<'info> {
    #[account(
        constraint = (
            !group.load()?.is_protocol_paused()
        ) @ MarginfiError::ProtocolPaused
    )]
    pub group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        has_one = mint @ MarginfiError::InvalidEmissionsMint,
        has_one = liquidity_vault @ MarginfiError::InvalidLiquidityVault,
        constraint = is_marginfi_asset_tag(bank.load()?.config.asset_tag)
            @ MarginfiError::WrongAssetTagForStandardInstructions,
    )]
    pub bank: AccountLoader<'info, Bank>,

    pub mint: InterfaceAccount<'info, Mint>,

    /// NOTE: This is a TokenAccount, spl transfer will validate it.
    ///
    /// CHECK: Account provided only for funding rewards
    #[account(mut)]
    pub emissions_funding_account: UncheckedAccount<'info>,

    #[account(mut)]
    pub depositor: Signer<'info>,

    #[account(mut)]
    pub liquidity_vault: Box<InterfaceAccount<'info, TokenAccount>>,

    pub token_program: Interface<'info, TokenInterface>,
}
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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1860)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
        bank.change_asset_shares(
            asset_shares_increase,
            matches!(operation_type, BalanceIncreaseType::BypassDepositLimit),
        )?;
```
