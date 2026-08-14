### Title
Depositors can frontrun permissionless `lending_pool_handle_bankruptcy` to dodge socialized bad-debt losses - ([File: programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs])

### Summary
The Frankencoin report describes `notifyLoss` immediately cutting share value, letting a depositor who has cleared the withdrawal cooldown frontrun the loss-realizing call (redeem, let the loss land on remaining holders, then re-deposit at the discounted rate). marginfi has a structurally identical mechanism: `lending_pool_handle_bankruptcy` → `Bank::socialize_loss` instantly and atomically slashes `asset_share_value` for every remaining depositor in a bank the moment bad debt is settled [1](#0-0) . Unlike Frankencoin's reserve, marginfi deposits have no cooldown at all — any depositor can withdraw and redeposit freely at any time [2](#0-1) , and when a bank has `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` set, anyone — not just an admin — can trigger `handle_bankruptcy`, making the loss-realization event publicly visible and triggerable in the mempool [3](#0-2) .

### Finding Description
`lending_pool_handle_bankruptcy` computes `bad_debt` for a bankrupt account, subtracts what the insurance fund can cover, and passes the remainder into `bank.socialize_loss(socialized_loss)` [4](#0-3) [5](#0-4) . `socialize_loss` reduces `asset_share_value` for the whole bank in a single atomic step — every existing depositor's shares are immediately worth less, with no time-weighting or amortization [6](#0-5) .

Because:
1. The instruction can be called permissionlessly once `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` is set on a bank [3](#0-2) , and
2. marginfi withdraw/deposit have no cooldown or delay (unlike Frankencoin's 90-day window) [2](#0-1) ,

any depositor who observes a pending bankruptcy transaction (or an account that is clearly bankrupt/eligible and about to be settled) can bundle: `withdraw (their full balance) → allow handle_bankruptcy to land → deposit back`. This lets them fully avoid absorbing their pro-rata share of `socialized_loss`, shifting that loss entirely onto other depositors who did not/could not withdraw in time. This is the exact same MEV pattern as the Frankencoin `notifyLoss` finding: an event that instantaneously and publicly slashes a shared, pooled share-value metric, combined with unrestricted entry/exit around that event. The project's own test suite explicitly flags awareness of this exposure ("The victim in this exploit is any depositor who will absorb the socialized loss") in the receivership/bankruptcy CPI test [7](#0-6) , although that specific test targets a different (CPI-forgery) attack vector, not the withdraw/redeposit frontrun.

### Impact Explanation
Successful exploitation causes an unauthorized transfer of value: the loss that should be borne pro-rata by all depositors of the bank is instead concentrated onto those who could not react in time, while the frontrunner exits and reenters with the same nominal token balance but without incurring the share-value haircut. This is a direct, protocol-level unfairness/loss-shifting bug affecting core accounting (`asset_share_value`) in the bankruptcy/loss-socialization path — not a validator, privileged-admin, or purely theoretical issue, since `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG`-enabled banks allow any unprivileged actor to trigger the event and any unprivileged depositor to game entry/exit around it.

### Likelihood Explanation
Likelihood is moderate: it requires (a) a bank with `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` enabled (or observation of an admin's pending `handle_bankruptcy` transaction in the mempool), and (b) a depositor actively monitoring the mempool/bankruptcy eligibility of accounts in that bank. Given documentation states "bankruptcy has never been executed in the main pool" as of writing [8](#0-7) , real-world occurrence is rare, but the underlying mechanism is fully reachable by any unprivileged user with no special access.

### Recommendation
Apply one of the standard MEV-resistant loss-accounting mitigations mentioned in the source report, adapted to marginfi's architecture:
1. Amortize `socialized_loss` over a short vesting window (writing down `asset_share_value` gradually rather than instantaneously) instead of applying the full write-down in a single atomic step in `Bank::socialize_loss`.
2. Add a short withdrawal delay/queue for large withdrawals so that a depositor's exit price reflects any bankruptcy events already queued/pending against the bank.
3. Restrict `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` usage or route `handle_bankruptcy` execution through a private/protected relay for the initial window, similar to the whitelist-bot suggestion in the source report, before opening it to the public mempool.

### Proof of Concept
1. Bank `X` has `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` set, and depositor A holds shares in `X`.
2. Attacker B monitors the mempool and sees a `lending_pool_handle_bankruptcy` transaction about to land against a bankrupt account in bank `X`, which will call `bank.socialize_loss(loss_amount)` and reduce `X.asset_share_value` [1](#0-0) .
3. B submits, immediately prior, a full withdrawal of their deposit position in bank `X` (no cooldown blocks this) [2](#0-1) .
4. `handle_bankruptcy` executes, calling `socialize_loss`, reducing `asset_share_value` for all remaining depositors, including A but excluding B (who already exited) [9](#0-8) .
5. B redeposits the same token amount into bank `X`, now minting more shares per token at the new, lower `asset_share_value`, capturing the discount that A and other passive depositors paid for.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L852-878)
```rust
    /// Socialize a loss of `loss_amount` among depositors, the `total_deposit_shares` stays the
    /// same, but total value of deposits is reduced by `loss_amount`;
    ///
    /// In cases where assets < liabilities, the asset share value will be set to zero, but cannot
    /// go negative. Effectively, depositors forfeit their entire deposit AND all earned interest in
    /// this case.
    fn socialize_loss(&mut self, loss_amount: I80F48) -> MarginfiResult<bool> {
        let mut kill_bank = false;
        let total_asset_shares: I80F48 = self.total_asset_shares.into();
        let old_asset_share_value: I80F48 = self.asset_share_value.into();

        // Compute total "old" value of shares
        let total_value: I80F48 = total_asset_shares
            .checked_mul(old_asset_share_value)
            .ok_or_else(math_error!())?;

        // Subtract loss, clamping at zero (i.e. assets < liabilities, the bank is wiped out)
        if total_value <= loss_amount {
            self.asset_share_value = I80F48::ZERO.into();
            // This state is irrecoverable, the bank is dead.
            kill_bank = true;
        } else {
            // otherwise subtract then redistribute
            let new_share_value: I80F48 = (total_value - loss_amount)
                .checked_div(total_asset_shares)
                .ok_or_else(math_error!())?;
            self.asset_share_value = new_share_value.into();
```

**File:** guides/DEVELOPERS_INTEGRATORS/ACCOUNT_LIFECYCLE.md (L116-119)
```markdown
### 1. Active

The normal state. The authority can freely deposit, withdraw, borrow, repay, and perform flash
loans. Health checks apply to operations that increase risk.
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L56-67)
```rust
        let is_admin_or_risk_admin = signer == group.risk_admin || signer == group.admin;
        let permissionless_bad_debt_settlement =
            bank.get_flag(PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG);

        if permissionless_bad_debt_settlement {
            // if permissionless, users can bankrupt reduce-only or operational banks
            validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;
        } else {
            // admin can bankrupt banks in any state
            validate_bank_state(&bank, InstructionKind::Unrestricted)?;
            check!(is_admin_or_risk_admin, MarginfiError::Unauthorized);
        }
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L122-147)
```rust
    let bad_debt: I80F48 =
        bank.get_liability_amount(lending_account_balance.liability_shares.into())?;

    check!(
        bad_debt > ZERO_AMOUNT_THRESHOLD,
        MarginfiError::BalanceNotBadDebt
    );

    let (covered_by_insurance, socialized_loss) = {
        let available_insurance_fund: I80F48 = maybe_bank_mint
            .as_ref()
            .map(|mint| {
                utils::calculate_post_fee_spl_deposit_amount(
                    mint.to_account_info(),
                    insurance_vault.amount,
                    clock.epoch,
                )
            })
            .transpose()?
            .unwrap_or(insurance_vault.amount)
            .into();

        let covered_by_insurance = min(bad_debt, available_insurance_fund);
        let socialized_loss = max(bad_debt - covered_by_insurance, I80F48::ZERO);

        (covered_by_insurance, socialized_loss)
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L189-199)
```rust
    // Socialize bad debt among depositors.
    let kill_bank = bank.socialize_loss(socialized_loss)?;

    // Settle bad debt.
    // The liabilities of this account and global total liabilities are reduced by `bad_debt`
    BankAccountWrapper::find(
        &bank_loader.key(),
        &mut bank,
        &mut marginfi_account.lending_account,
    )?
    .repay(bad_debt)?;
```

**File:** programs/marginfi/tests/user_actions/liquidate_receiver_cpi.rs (L210-222)
```rust
#[tokio::test]
async fn handle_bankruptcy_via_cpi_fails() -> anyhow::Result<()> {
    let test_f = TestFixture::new(Some(TestSettings::all_banks_payer_not_admin())).await;

    let sol_bank = test_f.get_bank(&BankMint::Sol);
    let usdc_bank = test_f.get_bank(&BankMint::Usdc);

    // The victim in this exploit is any depositor who will absorb the socialized loss
    let victim = test_f.create_marginfi_account().await;
    let victim_usdc_acc = test_f.usdc_mint.create_token_account_and_mint_to(200).await;
    victim
        .try_bank_deposit(victim_usdc_acc.key, usdc_bank, 100.0, None)
        .await?;
```

**File:** guides/RISK_AND_LIQUIDATORS/BANKRUPTCY.md (L56-58)
```markdown
### When Does This Matter?

Ideally, never. As of November 2025, bankruptcy has never been executed in the main pool.
```
