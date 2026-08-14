Based on my investigation, I found a valid analog in the marginfi-v2 Solend integration.

### Title
Manual, insufficiently-strict byte-offset parsing of Solend obligation collateral amount enables withdrawal accounting bypass / bad-debt creation - ([File: programs/solend-mocks/src/state.rs])

### Summary
`validate_solend_obligation` and `get_solend_obligation_deposit_amount` in `programs/solend-mocks/src/state.rs` parse a raw, unchecked Solend obligation account by manually indexing fixed byte offsets instead of using a strongly-typed/bounds-verified deserializer [1](#0-0) . This mirrors the "leniency in position-based parsing" bug class from the external report: the code trusts a single length check (`data.len() >= OBLIGATION_LEN`) and a couple of tag/version bytes, then reads fixed offsets (`deposit_start = 204`, `+32`, `+40`, etc.) without validating that those offsets correspond to a semantically-tagged/well-formed record, analogous to `get_body_hash` trusting `body_hash_index` without verifying the surrounding tag structure.

### Finding Description
`validate_solend_obligation` checks `data.len() >= OBLIGATION_LEN`, the version byte, `deposits_len == 1`, and `borrows_len == 0` [2](#0-1) , then reads the "first deposit" fields at hardcoded offsets 204–243 [3](#0-2) . `get_solend_obligation_deposit_amount`, used by both `solend_deposit` and `solend_withdraw` to compute `obligation_collateral_change` (the amount actually credited/debited from the marginfi position), performs the same raw offset read at `deposit_start + 32..deposit_start + 40` with only a length and version-byte check — it does **not** re-verify `deposits_len == 1` or `borrows_len == 0` [4](#0-3) .

This value directly drives real accounting: in `solend_deposit`, `obligation_collateral_change` (derived from this parse) is converted to `I80F48` and passed to `bank_account.deposit_no_repay`, which mints/increases the user's asset shares in the bank [5](#0-4) . In `solend_withdraw`, the same parse computes `obligation_collateral_change` used only to sanity-check `assert_within_one_token` against the requested `collateral_amount` — but the debit from marginfi's ledger itself is driven by `bank_account.withdraw`/`withdraw_all` calls made *before* this check, based on user input, not the raw obligation data [6](#0-5) .

The `integration_acc_2` (Solend obligation) is constrained only by `owner == SOLEND_PROGRAM_ID` and a `has_one` PDA match — its *contents* are never validated against a canonical/typed Solend account layout beyond these hand-picked offsets [7](#0-6) . If the Solend obligation account layout ever diverges from the hardcoded assumption (e.g., a version bump, alternate `deposits_len`/`borrows_len` combination not equal to `(1,0)` in `get_solend_obligation_deposit_amount`, or a legitimate multi-deposit/multi-borrow obligation reused for a bank), the parsed "deposit amount" at byte 236-243 no longer corresponds to the actual collateral value, since `get_solend_obligation_deposit_amount` re-derives it without re-checking those invariants that `validate_solend_obligation` enforces. This is the direct analog of the DKIM report's finding that step 4 (body hash extraction) trusted a position without the step-2/step-3 structural guarantees being re-verified at the point of use.

### Impact Explanation
If the obligation account's data ever deviates from the exact `(deposits_len=1, borrows_len=0)` shape assumed by `get_solend_obligation_deposit_amount` (which is not re-validated in that function, only in the separate `validate_solend_obligation`), the computed `obligation_collateral_change` used in `solend_deposit` to credit user shares via `deposit_no_repay` could be manipulated or miscomputed, leading to over-crediting of marginfi bank shares (unauthorized state change / potential insolvency) or a withdrawal accounting mismatch that masks abnormal Solend obligation states behind `assert_within_one_token`'s tolerance.

### Likelihood Explanation
Likelihood is moderate: `validate_solend_obligation` is called once at the top of both `solend_deposit`/`solend_withdraw` and does enforce `deposits_len==1`/`borrows_len==0` at call time, which narrows the exploit window. However, `get_solend_obligation_deposit_amount` is called multiple times (before and after the CPI) without re-checking these invariants, so any code path or CPI interaction that could transiently or permanently alter `deposits_len`/`borrows_len` between the initial validation and the final read (or any layout mismatch with actual on-chain Solend obligations) would go undetected.

### Recommendation
Use a proper zero-copy/typed Solend `Obligation` deserializer (or re-validate `deposits_len == 1 && borrows_len == 0` and full struct bounds) inside `get_solend_obligation_deposit_amount` itself, not only in the separate `validate_solend_obligation` helper, so every read of the collateral amount is guarded by the same structural invariants — mirroring the report's recommendation to tighten bounds/tag checks at the exact point of data extraction rather than relying on non-local reasoning from a separate validation step.

### Proof of Concept
Not independently reproducible from static analysis alone: exploitation would require constructing or influencing a Solend obligation account whose `data_flat` layout diverges from the hardcoded `(deposits_len=1, borrows_len=0, deposit at byte 204)` assumption while still passing the `owner == SOLEND_PROGRAM_ID` and `has_one` PDA constraints checked in `SolendDeposit`/`SolendWithdraw` [8](#0-7) . Given the reliance on Solend's actual account layout, full confirmation would need a Devin session with mainnet/testnet Solend obligation fixtures to determine whether such a divergent-but-owner-valid obligation state is reachable in practice.

### Citations

**File:** programs/solend-mocks/src/state.rs (L183-277)
```rust
pub fn validate_solend_obligation(account: &AccountInfo, expected_reserve: Pubkey) -> Result<()> {
    // Verify owner is Solend program
    require_keys_eq!(
        *account.owner,
        crate::ID,
        SolendMocksError::InvalidAccountData
    );

    let data = account.try_borrow_data()?;

    // Check size (including version byte)
    require!(
        data.len() >= OBLIGATION_LEN,
        SolendMocksError::InvalidAccountData
    );

    // Check version byte (first byte should be 1)
    require_eq!(data[0], 1u8, SolendMocksError::InvalidAccountData);

    // Manual validation without deserialization
    // Byte positions calculated from pack_into_slice in obligation.rs:
    //
    // mut_array_refs![output,
    //     1,        // version → Byte 0
    //     8,        // last_update_slot → Byte 1-8
    //     1,        // last_update_stale → Byte 9
    //     32,       // lending_market → Byte 10-41
    //     32,       // owner → Byte 42-73
    //     16,       // deposited_value → Byte 74-89
    //     16,       // borrowed_value → Byte 90-105
    //     16,       // allowed_borrow_value → Byte 106-121
    //     16,       // unhealthy_borrow_value → Byte 122-137
    //     16,       // borrowed_value_upper_bound → Byte 138-153
    //     1,        // borrowing_isolated_asset → Byte 154
    //     16,       // super_unhealthy_borrow_value → Byte 155-170
    //     16,       // unweighted_borrowed_value → Byte 171-186
    //     1,        // closeable → Byte 187
    //     14,       // _padding → Byte 188-201
    //     1,        // deposits_len → Byte 202
    //     1,        // borrows_len → Byte 203
    //     1096      // data_flat → Byte 204-1299
    // ];
    //
    // Within data_flat (starting at byte 204):
    // - deposits: deposits_len * 88 bytes each
    // - borrows: borrows_len * 112 bytes each
    // First deposit structure (88 bytes):
    // - deposit_reserve: Byte 204-235 (32 bytes)
    // - deposited_amount: Byte 236-243 (8 bytes)
    // - market_value: Byte 244-259 (16 bytes)
    // - padding: Byte 260-291 (32 bytes)
    //

    // Check deposits_len at position 202 (should be 1 for single deposit)
    require_eq!(
        data[202],
        1u8,
        SolendMocksError::InvalidObligationCollateral
    );

    // Check borrows_len at position 203 (should be 0 for no borrows)
    require_eq!(data[203], 0u8, SolendMocksError::InvalidObligationLiquidity);

    // First deposit starts at position 204 in data_flat array
    // Each deposit is 88 bytes: [Pubkey (32) + u64 (8) + u128 (16) + padding (32)]
    let deposit_start = 204;

    // Check first deposit reserve matches expected (32 bytes)
    let deposit_reserve_bytes = &data[deposit_start..deposit_start + 32];
    let deposit_reserve = Pubkey::try_from(deposit_reserve_bytes)
        .map_err(|_| SolendMocksError::InvalidObligationCollateral)?;
    require_keys_eq!(
        deposit_reserve,
        expected_reserve,
        SolendMocksError::InvalidObligationCollateral
    );

    // Check first deposit amount is non-zero (8 bytes at position 236-243)
    let deposit_amount_bytes = &data[deposit_start + 32..deposit_start + 40];
    let deposit_amount = u64::from_le_bytes(
        deposit_amount_bytes
            .try_into()
            .map_err(|_| SolendMocksError::InvalidObligationCollateral)?,
    );
    require!(
        deposit_amount > 0,
        SolendMocksError::InvalidObligationCollateral
    );

    // Since deposits_len = 1, we don't need to check other deposits
    // The dataFlat buffer only contains exactly 1 deposit (88 bytes)
    // followed by 0 borrows, so there are no other deposits to validate

    Ok(())
}
```

**File:** programs/solend-mocks/src/state.rs (L279-313)
```rust
/// Get the deposit amount at position 0 from a Solend obligation
pub fn get_solend_obligation_deposit_amount(account: &AccountInfo) -> Result<u64> {
    // Verify owner is Solend program
    require_keys_eq!(
        *account.owner,
        crate::ID,
        SolendMocksError::InvalidAccountData
    );

    let data = account.try_borrow_data()?;

    // Check size (including version byte)
    require!(
        data.len() >= OBLIGATION_LEN,
        SolendMocksError::InvalidAccountData
    );

    // Check version byte
    require_eq!(data[0], 1u8, SolendMocksError::InvalidAccountData);

    // Manual extraction without deserialization
    // First deposit starts at position 204 in data_flat array
    // Each deposit is 88 bytes: [Pubkey (32) + u64 (8) + u128 (16) + padding (32)]
    let deposit_start = 204;

    // Get first deposit amount (8 bytes at position 236-243)
    let deposit_amount_bytes = &data[deposit_start + 32..deposit_start + 40];
    let deposit_amount = u64::from_le_bytes(
        deposit_amount_bytes
            .try_into()
            .map_err(|_| SolendMocksError::InvalidObligationCollateral)?,
    );

    Ok(deposit_amount)
}
```

**File:** programs/marginfi/src/instructions/solend/deposit.rs (L81-108)
```rust
    let final_obligation_deposited_amount =
        get_solend_obligation_deposit_amount(&ctx.accounts.integration_acc_2)?;

    // Verify the deposit was successful by checking obligation balance increased by correct amount
    let obligation_collateral_change =
        final_obligation_deposited_amount - initial_obligation_deposited_amount;

    assert_within_one_token(
        obligation_collateral_change,
        expected_collateral_amount,
        MarginfiError::SolendDepositFailed,
    )?;

    {
        let mut bank = ctx.accounts.bank.load_mut()?;
        let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
        let group = ctx.accounts.group.load()?;
        let clock = Clock::get()?;

        let mut bank_account = BankAccountWrapper::find_or_create(
            &ctx.accounts.bank.key(),
            &mut bank,
            &mut marginfi_account.lending_account,
        )?;

        // Convert deposit amount to I80F48 for calculations
        let obligation_collateral_change_i80f48 = I80F48::from_num(obligation_collateral_change);
        let share_amount = bank_account.deposit_no_repay(obligation_collateral_change_i80f48)?;
```

**File:** programs/marginfi/src/instructions/solend/deposit.rs (L201-221)
```rust
    /// The Solend obligation account
    /// CHECK: Validated in instruction body
    #[account(
        mut,
        constraint = integration_acc_2.owner == &SOLEND_PROGRAM_ID @ MarginfiError::InvalidSolendAccount
    )]
    pub integration_acc_2: UncheckedAccount<'info>,

    /// CHECK: validated by the Solend program
    pub lending_market: UncheckedAccount<'info>,

    /// Derived from the lending market
    /// CHECK: validated by the Solend program
    pub lending_market_authority: UncheckedAccount<'info>,

    /// The Solend reserve that holds liquidity
    #[account(
        mut,
        constraint = !integration_acc_1.load()?.is_stale()? @ MarginfiError::SolendReserveStale
    )]
    pub integration_acc_1: AccountLoader<'info, SolendMinimalReserve>,
```

**File:** programs/marginfi/src/instructions/solend/withdraw.rs (L112-187)
```rust
        let mut bank_account =
            BankAccountWrapper::find(&bank_key, &mut bank, &mut marginfi_account.lending_account)?;

        let (collateral_amount, share_amount) = if withdraw_all {
            bank_account.withdraw_all(in_receivership)?
        } else {
            let share_amount = bank_account.withdraw(I80F48::from_num(amount))?;
            (amount, share_amount)
        };

        // Rate limiting tracks net outflow; skip for flashloan/liquidation/deleverage flows.
        let rate_limit_amount = if withdraw_all {
            collateral_amount
        } else {
            amount
        };
        record_withdrawal_outflow(
            group_rate_limit_enabled,
            rate_limit_amount,
            rate_limit_amount,
            price,
            &mut bank,
            &group,
            ctx.accounts.group.key(),
            ctx.accounts.bank.key(),
            &marginfi_account,
            &clock,
        )?;

        // Track withdrawal limit for risk admin during deleverage
        if marginfi_account.get_flag(ACCOUNT_IN_DELEVERAGE) {
            let withdrawn_equity = calc_value(
                I80F48::from_num(collateral_amount),
                price,
                bank.get_balance_decimals(),
                None,
            )?;
            group.check_deleverage_withdraw_limit(withdrawn_equity, clock.unix_timestamp)?;
            emit!(DeleverageWithdrawFlowEvent {
                group: ctx.accounts.group.key(),
                bank: ctx.accounts.bank.key(),
                mint: bank.mint,
                outflow_usd: withdrawn_equity.to_num(),
                current_timestamp: clock.unix_timestamp,
            });
        }

        (collateral_amount, share_amount)
    };

    // Get initial obligation data to verify withdrawal amount later
    let initial_obligation_deposited_amount =
        get_solend_obligation_deposit_amount(&ctx.accounts.integration_acc_2)?;

    // Get initial values to verify successful withdrawal later
    let pre_transfer_vault_balance =
        accessor::amount(&ctx.accounts.liquidity_vault.to_account_info())?;
    let expected_liquidity_amount = ctx
        .accounts
        .integration_acc_1
        .load()?
        .collateral_to_liquidity(collateral_amount)?;

    ctx.accounts
        .cpi_solend_withdraw(collateral_amount, authority_bump)?;

    // Verify the obligation deposit amount decreased by the correct amount
    let final_obligation_deposited_amount =
        get_solend_obligation_deposit_amount(&ctx.accounts.integration_acc_2)?;
    let obligation_collateral_change =
        initial_obligation_deposited_amount - final_obligation_deposited_amount;
    assert_within_one_token(
        obligation_collateral_change,
        collateral_amount,
        MarginfiError::SolendWithdrawFailed,
    )?;
```
