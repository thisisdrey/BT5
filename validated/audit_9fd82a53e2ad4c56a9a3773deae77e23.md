Confirmed: `LendingPoolEmissionsDeposit` has no privileged-role restriction beyond a valid `Signer` — anyone can be the `depositor`, and there is no cooldown, lockup, or per-block cap on the amount distributed. This confirms the analog is unprivileged-reachable and structurally identical to the uncapped/no-timing-protection version of the Angle `_updateSanRate()` bug.

### Title
Emissions Reward Sandwiching via Instant Deposit/Withdraw Dilutes Legitimate Depositors' Share of `lending_pool_emissions_deposit` - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
`lending_pool_emissions_deposit` distributes a fixed `amount` of reward tokens to all current holders of a bank's asset shares by directly recomputing `asset_share_value = (total_assets + amount) / total_asset_shares` at call time [1](#0-0) . This is structurally identical to Angle's vulnerable `_updateSanRate()` pattern: a fixed reward pool split pro-rata against a total-supply/total-shares figure that can be manipulated in the same instant, with no minimum holding period and no cap analogous to `maxInterestsDistributed`.

### Finding Description
The instruction is fully permissionless — `depositor` is just `Signer<'info>` with no role check, and `emissions_funding_account` is an arbitrary unchecked token account [2](#0-1) . Distribution math:

```rust
let total_assets = bank.get_asset_amount(total_asset_shares)?;
let updated_total_assets = total_assets.checked_add(I80F48::from_num(amount))...;
bank.asset_share_value = updated_total_assets.checked_div(total_asset_shares)...;
``` [1](#0-0) 

`total_asset_shares` is read live at call time (line 111) with no time-weighting, no snapshot from a prior block, and no cap on `amount` relative to existing TVL [3](#0-2) . This exactly mirrors the two root causes in the Angle report: (1) no minimum holding period before becoming eligible for the reward, and (2) no fixed-quantity cap on the amount distributed per call (Angle's fix replaced a percentage-of-supply cap with `maxInterestsDistributed`, a fixed token cap — no equivalent exists here at all, so marginfi's version has strictly weaker protection than even the pre-patch Angle code).

An unprivileged attacker can:
1. Deposit a very large amount of the bank's underlying asset (via `lending_account_deposit`, optionally capital-sourced from an external flashloan protocol, or marginfi's own `lending_account_start_flashloan`/`lending_account_borrow` on another bank swapped into this asset) immediately before a scheduled/known `lending_pool_emissions_deposit` call. Per `guides/USER/EMISSIONS.md`, emissions/reward top-ups are delivered on a predictable cadence ("typically on Wednesday") [4](#0-3) , making the timing of the third-party call predictable and thus front-runnable/sandwichable.
2. Let the legitimate reward provider's `lending_pool_emissions_deposit(amount)` transaction land, which raises `asset_share_value` uniformly for all current shareholders, including the attacker's just-deposited, disproportionately large share.
3. Immediately withdraw, realizing a share of `amount` proportional to `attacker_shares / total_shares_after_inflation`, which approaches 100% as the attacker's deposit size grows relative to existing TVL.

Because the reward pool `amount` is a fixed quantity independent of who holds shares, inflating `total_asset_shares` right before the call directly steals reward value that should have accrued to genuine, long-term depositors — precisely the mechanism described in the Angle report's "second issue" (percentage/pro-rata cap defeated by flash-inflating the denominator), except here there is no cap whatsoever.

### Impact Explanation
This allows an unprivileged attacker to extract most or all of a reward/emissions deposit intended for genuine depositors, at negligible cost (transaction fees plus, if using marginfi's own flashloan, no fee at all since "No fees are charged for using this service"). This is a direct unauthorized transfer of value away from legitimate depositors to the attacker — a concrete theft vector reachable without any privileged role.

### Likelihood Explanation
Likelihood is moderate-to-high: the instruction is permissionless and callable by any bot/sponsor on a predictable cadence (per the emissions guide), giving attackers a foreseeable timing window. Executing the sandwich requires only ordinary deposit/withdraw capability (no admin or validator privilege) and, at most, use of marginfi's already-permissionless flashloan feature or externally sourced capital.

### Recommendation
Introduce a minimum holding-period requirement (e.g., shares must have been held for at least N slots/seconds, or exclude shares deposited in the same slot as the emissions call) before they qualify for reward distribution, mirroring Angle's `lastBlockUpdated` fix. Additionally, cap `amount` distributed per call in `lending_pool_emissions_deposit` as a function of a time-weighted/snapshotted share balance rather than the live `total_asset_shares`, or require the transfer per call to be batched/streamed over time rather than applied atomically to the current share value.

### Proof of Concept
1. Attacker observes (or predicts, per the documented weekly cadence) that a reward sponsor is about to call `lending_pool_emissions_deposit(amount)` for bank `B`.
2. In the preceding transaction/slot, attacker calls `lending_account_deposit` into `B` with a very large `deposit_amount` (sourced via flashloan if needed), acquiring `deposit_amount / asset_share_value` shares — dwarfing existing `total_asset_shares`.
3. The sponsor's `lending_pool_emissions_deposit(amount)` transaction executes, recomputing `asset_share_value = (total_assets + amount) / total_asset_shares` per lines 138-146 of `configure_bank.rs`, uniformly raising the value of all shares, including the attacker's newly inflated holding.
4. Attacker immediately calls `lending_account_withdraw` for their full balance, realizing `amount * (attacker_shares / total_shares)` — approaching the full `amount` as `deposit_amount` grows — while genuine long-term depositors receive a diminished share of the intended reward.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L111-116)
```rust
    let total_asset_shares = I80F48::from(bank.total_asset_shares);
    check!(
        total_asset_shares > I80F48::ZERO,
        MarginfiError::EmissionsUpdateError
    );

```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L138-146)
```rust
    let total_assets = bank.get_asset_amount(total_asset_shares)?;
    let updated_total_assets = total_assets
        .checked_add(I80F48::from_num(amount))
        .ok_or_else(math_error!())?;

    bank.asset_share_value = updated_total_assets
        .checked_div(total_asset_shares)
        .ok_or_else(math_error!())?
        .into();
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L177-187)
```rust
    pub mint: InterfaceAccount<'info, Mint>,

    /// NOTE: This is a TokenAccount, spl transfer will validate it.
    ///
    /// CHECK: Account provided only for funding rewards
    #[account(mut)]
    pub emissions_funding_account: UncheckedAccount<'info>,

    #[account(mut)]
    pub depositor: Signer<'info>,

```

**File:** guides/USER/EMISSIONS.md (L19-21)
```markdown
Emissions/incentives are delivered by airdrop to the Account's authority, typically on Wednesday, in
no particular order. In the above example, User 1 would get $0.5 + 0.5 * 0.143 * 5 = 1.715$ tokens
and User 2 would get $0.5 + 0.5 + 0.857 * 5 = 5.285$ tokens
```
