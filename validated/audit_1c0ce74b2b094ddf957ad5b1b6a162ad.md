### Title
Depositors can front-run `lending_pool_handle_bankruptcy` to avoid socialized bad-debt losses, concentrating the loss on remaining lenders - ([File: programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs])

### Summary
The Rio report shows stakers escaping validator penalties by withdrawing before an EigenLayer state update realizes the loss on-chain. Marginfi has a structurally identical window: once a marginfi account becomes bankrupt (bad debt exists on a bank), that bad debt is only actually applied to the bank's pool via a separate, delayed, publicly-visible instruction — `lending_pool_handle_bankruptcy` — which calls `Bank::socialize_loss`. Any depositor in that bank can see the pending bad debt and withdraw liquidity before `handle_bankruptcy` lands, escaping their proportional share of the loss and forcing it onto the depositors who remain.

### Finding Description
`lending_pool_handle_bankruptcy` computes the amount of bad debt not covered by insurance and calls `bank.socialize_loss(socialized_loss)`: [1](#0-0) 

`socialize_loss` divides the fixed `loss_amount` across whatever `total_asset_shares` remain in the bank *at the moment it executes*, lowering `asset_share_value` for all current shareholders: [2](#0-1) 

The guide documents this exact mechanic and explicitly calls out that it "socializes the loss to all remaining depositors": [3](#0-2) 

The bad debt itself becomes bankrupt-eligible (and thus publicly detectable on-chain) as soon as a user's liabilities exceed assets after liquidation — this is a separate, earlier state than the `handle_bankruptcy` transaction that actually mutates `asset_share_value`. Between these two events there is an unbounded, attacker-controlled delay: `handle_bankruptcy` can be called by anyone when `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` is set, or otherwise only by admin/risk_admin, but nothing prevents ordinary depositors from withdrawing from the same bank in the meantime: [4](#0-3) 

`lending_account_withdraw` performs no check for a pending/known bankruptcy in the bank and is available in `Operational` and `ReduceOnly` states — i.e. exactly the states a bank sits in while an unresolved bad debt exists: [5](#0-4) 

Because `socialize_loss` uses the *current* `total_asset_shares`/`total_value` (post-withdrawal) rather than a value snapshotted when the bad debt was created, any depositor who withdraws before `handle_bankruptcy` executes:
1. Fully avoids the loss (receives 1:1 redemption at the pre-loss `asset_share_value`), and
2. Shrinks the denominator over which the fixed `socialized_loss` amount is divided, so the *same absolute* bad debt is now spread over fewer remaining shares — raising the percentage loss borne by depositors who do not withdraw in time. In the limit, this can consume the entire remaining pool and trigger `kill_bank` (bank wiped to `asset_share_value = 0`, `KilledByBankruptcy`), whereas a fair pro-rata split at the time the bad debt was created would not have caused the wipe-out.

This is precisely the loss-redistribution mechanic that caused Sherlock to accept the escalation and rate M-7 (originally styled as Medium/High) in the Rio report: the vulnerability is not that TVL/asset value drops — it is that unequal timing of withdrawals relative to a known, pending loss-realization event causes non-proportional loss allocation among otherwise pari passu depositors.

### Impact Explanation
- Unprivileged depositors who monitor bank/account state (trivial on a public chain — bad debt is visible the moment a liquidated account's assets hit zero while liabilities remain) can withdraw ahead of `handle_bankruptcy` and pay none of the loss.
- Depositors who do not react in time absorb a disproportionately larger haircut than the fair, pro-rata share of the bad debt — up to and including full loss of their deposit (`kill_bank` / `KilledByBankruptcy`), which is a permanent, irrecoverable state per `guides/ADMIN/BANK_STATE.md`.
- This directly reduces the fairness/solvency guarantee of the socialization mechanism and can cause the bank to be killed (permanent, protocol-level state change / freeze of all remaining depositors' funds) in scenarios where a fair allocation would not have caused insolvency.

### Likelihood Explanation
Requires only: (1) an existing bad-debt/bankrupt account within a bank (already a documented, expected — if rare — occurrence per `BANKRUPTCY.md`), and (2) sufficient idle liquidity in that bank's `liquidity_vault` for depositors to withdraw before `handle_bankruptcy` lands. Because bad debt is visible on-chain as soon as it exists (well before the discretionary/admin-or-permissionless `handle_bankruptcy` call), and because `lending_account_withdraw` places no restriction tied to pending bankruptcy, this is straightforward for any unprivileged actor (or even automated bots) to execute without needing privileged access. Likelihood is similar to the original finding: low-to-moderate frequency (bankruptcy itself is rare, "has never been executed in the main pool" per the guide) but fully permissionless and mechanically trivial once the precondition (bad debt exists + liquid vault) is met.

### Recommendation
When a bank has an outstanding bankrupt/bad-debt account, snapshot the loss-sharing base (`total_asset_shares`/`total_value`) at the time the bad debt is recognized (e.g., when the liquidated account crosses into bankruptcy), rather than at the time `handle_bankruptcy` executes, so that depositors who withdraw during the intervening window still bear their pro-rata share of the loss (e.g., via a withdrawal fee/haircut proportional to outstanding unresolved bad debt, or by restricting/delaying withdrawals from a bank with known unresolved bad debt until `handle_bankruptcy` is settled).

### Proof of Concept
Conceptual reproduction using existing test scaffolding (`programs/marginfi/tests/admin_actions/bankruptcy.rs`):
1. Set up a bank with multiple depositors (as in `marginfi_group_handle_bankruptcy_success_not_insured_3_depositors`).
2. Have a borrower go bankrupt via `nullify_assets_for_bank` (simulating a liquidation that leaves bad debt) — at this point bad debt exists but `handle_bankruptcy` has not run yet, matching the state visible on-chain in the wild.
3. Before calling `try_handle_bankruptcy`, have one depositor withdraw their full deposit (`try_bank_withdraw` / `withdraw_all`), reducing `total_asset_shares`.
4. Call `try_handle_bankruptcy` on the bank as in the existing test.
5. Compare `asset_share_value` and remaining depositors' redeemable value against the "no front-run" baseline (as computed in the existing test at lines 962-980): the front-running depositor exits at par, while the remaining depositors absorb a larger-than-fair-share haircut than `bad_debt / total_asset_shares_at_bankruptcy_time` would imply. [6](#0-5)

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L122-148)
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
    };
```

**File:** programs/marginfi/src/state/bank.rs (L858-886)
```rust
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
            // Sanity check: should be unreachable.
            if new_share_value == I80F48::ZERO {
                kill_bank = true;
            }
        }

        Ok(kill_bank)
    }
```

**File:** guides/RISK_AND_LIQUIDATORS/BANKRUPTCY.md (L27-46)
```markdown
## Discharging a Bankruptcy

First, liquidators consume all the remaining assets that the user has. If the user has A dollars in
assets and B dollars in liabilities (in equity value, i.e. excluding any weights), we know that B >
A. After liquidation is complete, A_new = 0, and B_new = B - A + X, where X is the liquidation
premium and insurance.

Run `collect_bank_fees` before beginning the next step so the insurance fund is fully capitalized.

Next, the group administrator runs `handle_bankruptcy` on the user. For banks where
`PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG` is enabled, anyone can do this. This will perform the
following logic:

* If bank's insurance fund > liabilities, then the insurance fund is used to repay the user's liability.
* If the bank's insurance fund is not sufficient, the remainder will be covered by taking liquidity
  out of the bank, reducing the asset share value. This socializes the loss to all remaining
  depositors.
* If the bank's insurance fund and liquidity are not sufficient (super-bankruptcy), the bank is
  killed. The asset share value is set to zero, wiping out all holdings for all other depositors.
  This state is irrecoverable, and the bank is permanently disabled.
```

**File:** guides/ADMIN/BANK_STATE.md (L90-97)
```markdown
### Permissionless Bad Debt Settlement (Bit 2)

- **Bit 2** (`PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG`, value 4): When set, anyone can call the
  `handle_bankruptcy` instruction for this bank. When not set, only the `risk_admin` or `admin`
  can do so.

This is useful for banks where you want the community or bots to be able to settle bad debt without
waiting for the risk admin.
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L45-75)
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
```

**File:** programs/marginfi/tests/admin_actions/bankruptcy.rs (L950-980)
```rust
    borrower_mfi_account_f
        .nullify_assets_for_bank(sol_bank_f.key)
        .await?;

    test_f
        .marginfi_group
        .try_handle_bankruptcy(usdc_bank_f, &borrower_mfi_account_f)
        .await?;

    let borrower_mfi_account = borrower_mfi_account_f.load().await;
    let borrower_usdc_balance = borrower_mfi_account.lending_account.balances[1];

    assert_eq!(
        I80F48::from(borrower_usdc_balance.liability_shares),
        I80F48::ZERO
    );

    let lender_1_mfi_account = lender_1_mfi_account_f.load().await;
    let usdc_bank = usdc_bank_f.load().await;

    let lender_usdc_value = usdc_bank.get_asset_amount(
        lender_1_mfi_account.lending_account.balances[0]
            .asset_shares
            .into(),
    )?;

    assert_eq_noise!(
        lender_usdc_value,
        I80F48::from(native!(96_666, "USDC")),
        I80F48::from(native!(1, "USDC"))
    );
```
