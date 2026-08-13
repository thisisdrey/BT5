This is exactly the analog: an admin-controlled "reward accumulation" mechanism can, when it exceeds a fixed number of slots, permanently make the withdraw path revert for an unprivileged user — the same root-cause shape as the ComplexRewarder issue (a secondary reward accounting path failing and reverting the primary user withdraw flow).

### Title
Excess Drift admin reward deposits permanently brick user withdrawals (freeze of funds) - ([File: programs/marginfi/src/instructions/drift/withdraw.rs])

### Summary
`drift_withdraw` enforces a hard-coded constraint (`validate_not_bricked_by_admin_deposits`) that only tolerates up to 3 active Drift spot positions (1 main asset + 2 reward slots) and only exposes 2 optional reward-account slots (`drift_reward_spot_market`, `drift_reward_spot_market_2`) in the instruction's accounts. If Drift admin deposits push the user's Drift account past this limit, the `DriftWithdraw` account constraint unconditionally fails and the user can never withdraw their principal, regardless of what accounts they pass.

### Finding Description
`DriftWithdraw`'s `integration_acc_2` account carries an Anchor constraint that calls `validate_not_bricked_by_admin_deposits()`, which errors with `DriftMocksError::TooManyActiveDeposits` whenever `active_deposits > 3` [1](#0-0) . The check counts active Drift spot positions with nonzero deposit balance [2](#0-1) . `drift_withdraw`'s account schema only ever provides two optional reward slots (`drift_reward_spot_market` and `drift_reward_spot_market_2`) [3](#0-2) , so once Drift protocol admins add a third (or more) admin-deposit reward position to the liquidity-vault-authority's Drift `User` account, `active_deposits` becomes 4+, `validate_not_bricked_by_admin_deposits` always returns `TooManyActiveDeposits`, and the constraint fails on every call to `drift_withdraw` — there is no combination of accounts the caller can pass to satisfy it. The mitigation path (fee admin harvesting the excess reward positions via `drift_harvest_reward`) is a separate, permissionless-but-admin-triggered instruction [4](#0-3) , and nothing in `drift_withdraw` itself invokes or depends on harvesting succeeding before checking the deposit count — so if harvesting is not performed (or a specific reward mint/market cannot be harvested for any reason), user withdrawals for that Drift-backed bank remain permanently blocked.

This mirrors the TRST-H-2 pattern: a secondary/child reward-accounting mechanism (Drift's admin-deposit reward positions, analogous to child rewarders) can fail/overflow independently of the user's own action, and that failure unconditionally reverts the primary user-facing action (`withdraw()`), risking a freeze of user funds.

### Impact Explanation
If the number of admin-deposited Drift reward positions on the shared `liquidity_vault_authority` Drift `User` account exceeds 3, every unprivileged user with a deposit in that Drift-backed marginfi bank is unable to withdraw their principal through `drift_withdraw` until an admin harvests the excess reward positions down to ≤2. This is a permanent-lock/freeze-of-funds condition scoped to normal user withdraw calls in a core money-movement path (bank withdraw via a supported external-venue integration).

### Likelihood Explanation
Because `liquidity_vault_authority` and its associated Drift `User`/`UserStats` accounts are shared across all marginfi depositors in a given Drift bank, this only requires the Drift protocol (or another actor able to create admin deposits at Drift position indices 2-7) to add more than 2 concurrent reward-type deposits to that single shared account — a plausible/likely event outside marginfi's control, not requiring any marginfi-side privileged action. Compared with the original ComplexRewarder bug (rejected because child rewarders were unused/out of scope), this is a live, in-scope mechanism (Drift admin rewards are explicitly supported/tested in this codebase), making the likelihood non-theoretical.

### Recommendation
Do not hard-fail the user's own principal withdrawal based on the state of a shared reward-tracking mechanism. Either: (1) decouple the health/validity check so `drift_withdraw` can proceed for the user's own market position even when unrelated admin reward positions exceed the supported slot count (e.g., ignore reward positions entirely for the "can I withdraw my principal" check, and require harvesting only for reward accounting, not to unblock withdraws), or (2) support a variable/unbounded number of reward accounts via `remaining_accounts` rather than a fixed 2-slot cap, or (3) add a permissionless "force-harvest N reward positions" capability that any caller (not just an admin flow) can invoke to unblock withdrawal deterministically.

### Proof of Concept
1. Marginfi group admin creates a Drift-backed bank; multiple users deposit into it via `drift_deposit`.
2. Drift admin (or Drift itself) performs 3+ separate admin reward deposits into the shared `liquidity_vault_authority`'s Drift `User` account at spot indices 2-7, in addition to the user's main asset position — pushing `count_active_deposits()` to 4.
3. Any user calls `drift_withdraw` (with or without both `drift_reward_spot_market`/`drift_reward_spot_market_2` supplied) — the `integration_acc_2` account constraint calling `validate_not_bricked_by_admin_deposits()` fails with `TooManyActiveDeposits` in all cases, since the instruction only supports 2 reward slots [5](#0-4) .
4. Until the global fee admin issues `drift_harvest_reward` enough times to bring active deposits back to ≤3, all withdrawals from that bank revert — freezing user funds in a path the ordinary unprivileged user cannot resolve themselves.

### Citations

**File:** programs/marginfi/src/instructions/drift/withdraw.rs (L401-409)
```rust
        constraint = {
            let user = integration_acc_2.load()?;
            user.validate_reward_accounts(
                drift_reward_spot_market.is_none(),
                drift_reward_spot_market_2.is_none(),
            ).is_ok()
        } @ MarginfiError::DriftMissingRewardAccounts,
        constraint = integration_acc_2.load()?.validate_not_bricked_by_admin_deposits().is_ok() @ MarginfiError::DriftBrickedAccount
    )]
```

**File:** programs/marginfi/src/instructions/drift/withdraw.rs (L430-447)
```rust
    /// Optional: Oracle for first reward asset (only needed if rewards exist)
    /// CHECK: validated by Drift program
    pub drift_reward_oracle: Option<UncheckedAccount<'info>>,

    /// Optional: Spot market for first reward asset (only needed if rewards exist)
    /// CHECK: validated by Drift program
    pub drift_reward_spot_market: Option<UncheckedAccount<'info>>,

    /// Optional: Mint for first reward asset (only needed if rewards exist)
    /// CHECK: validated by Drift program
    pub drift_reward_mint: Option<UncheckedAccount<'info>>,

    /// Optional: Oracle for second reward asset (backup in case multiple rewards)
    /// CHECK: validated by Drift program
    pub drift_reward_oracle_2: Option<UncheckedAccount<'info>>,

    /// Optional: Spot market for second reward asset (backup in case multiple rewards)
    /// CHECK: validated by Drift program
```

**File:** programs/drift-mocks/src/state.rs (L251-290)
```rust
impl MinimalUser {
    pub fn count_active_deposits(&self) -> usize {
        self.spot_positions
            .iter()
            .filter(|pos| pos.scaled_balance > 0 && pos.balance_type == SpotBalanceType::Deposit)
            .count()
    }

    fn get_active_deposit_markets(&self) -> Vec<u16> {
        self.spot_positions
            .iter()
            .filter(|pos| pos.scaled_balance > 0 && pos.balance_type == SpotBalanceType::Deposit)
            .map(|pos| pos.market_index)
            .collect()
    }

    /// Check if Drift has bricked this account with excessive admin deposits
    /// We support 1 main asset + up to 2 reward assets (3 total active deposits)
    /// If Drift admin deposited more reward assets, the account cannot withdraw
    pub fn validate_not_bricked_by_admin_deposits(&self) -> Result<()> {
        let active_deposits = self.count_active_deposits();

        if active_deposits > 3 {
            msg!(
                "ERROR: Drift has {} active deposit positions",
                active_deposits
            );
            msg!(
                "Active market indexes: {:?}",
                self.get_active_deposit_markets()
            );
            msg!("This account has been bricked by Drift admin deposits!");
            msg!("Cannot withdraw when more than 3 assets have active balances");
            msg!("We support 1 main asset + up to 2 reward assets");
            msg!("SOLUTION: Fee admin wallet needs to harvest these rewards ASAP!");
            return Err(DriftMocksError::TooManyActiveDeposits.into());
        }

        Ok(())
    }
```

**File:** programs/marginfi/src/instructions/drift/harvest_reward.rs (L18-44)
```rust
/// Harvest rewards from admin deposits in Drift spot markets
/// This instruction allows withdrawing from positions that were created by admin deposits
/// at indices 2-7 (index 0 is for USDC, 1 is for any other token mint)
/// Has a number of checks to ensure this only withdraws rewards
/// - Checks that harvest spot market does not match the bank's spot market
/// - Checks that harvest spot market mint does not match bank's mint
/// - Checks that the harvest spot market has a balance in index 2 - 7 on the user account
///   The only possible exception to index 2-7 is if someone rewards USDC usage which is unlikely.
///
/// Remaining accounts should be passed in the order required by Drift's withdraw instruction:
/// 1. Oracle accounts (optional)
/// 2. Spot market accounts (always required)
/// 3. Token mint (required for Token-2022)
pub fn drift_harvest_reward<'info>(
    ctx: Context<'info, DriftHarvestReward<'info>>,
) -> MarginfiResult {
    let spot_market_index = {
        let harvest_spot_market = ctx.accounts.harvest_drift_spot_market.load()?;
        harvest_spot_market.market_index
    };

    ctx.accounts
        .cpi_withdraw_from_position(spot_market_index, ctx.remaining_accounts)?;

    ctx.accounts.cpi_transfer_to_destination()?;
    Ok(())
}
```
