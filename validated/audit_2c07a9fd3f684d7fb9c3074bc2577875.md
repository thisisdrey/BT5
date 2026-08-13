### Title
Permissionless `lending_pool_emissions_deposit` enables a share-value inflation (donation) attack analogous to the Pool `burn`-then-donate exploit - (File: `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`)

### Summary
The reported Spartan Pool bug lets an attacker deflate a pool's `totalSupply` (via `burn`) so a subsequent, unprivileged deposit rounds to zero LP units, letting the attacker capture all value. Marginfi's analog is the `lending_pool_emissions_deposit` instruction, which is explicitly documented as "permissionless" and lets *any* signer push additional underlying tokens directly into a bank's `liquidity_vault` and inflate `asset_share_value` without minting new `total_asset_shares`. When a bank has a tiny, attacker-controlled `total_asset_shares` (e.g., the attacker is first/sole depositor), this instruction lets the attacker inflate `asset_share_value` arbitrarily, causing later depositors' `get_asset_shares` conversion to round toward zero shares while their tokens are still pulled into the vault - the same "small `P` / large unit-loss" rounding-abuse root cause as the Spartan report.

### Finding Description
`asset_share_value` and `asset_shares` work like a share/vault ratio, similar to the Spartan Pool's `totalSupply`-based unit formula: `get_asset_shares` divides the deposited value by `asset_share_value` [1](#0-0) , and `get_asset_amount` multiplies shares back by that same value [2](#0-1) .

`lending_pool_emissions_deposit` is callable by any signer (`depositor: Signer<'info>`, no admin check) as long as `total_asset_shares > 0` [3](#0-2) . It transfers `amount` tokens straight into `liquidity_vault` and recomputes `asset_share_value = (total_assets + amount) / total_asset_shares` — i.e., it inflates the share value without minting shares to the depositor [4](#0-3) .

Attack path:
1. Attacker creates/uses a bank where they are effectively the sole (or dominant) depositor, so `total_asset_shares` is tiny.
2. Attacker calls `lending_pool_emissions_deposit` with a large `amount`, inflating `asset_share_value` to a very large number while `total_asset_shares` stays unchanged.
3. A victim then calls `lending_account_deposit`, which computes `asset_shares_increase = bank.get_asset_shares(asset_amount_increase)` [5](#0-4) . Because `asset_share_value` is now huge relative to the victim's deposit, the resulting share amount rounds down toward zero/near-zero in I80F48 fixed-point precision, while the victim's tokens are still transferred into the (now attacker-influenced) `liquidity_vault`.
4. The attacker, holding effectively all real shares, can then withdraw/`withdraw_all`, redeeming the inflated `asset_share_value` against their (unchanged) shares and capturing the victim's deposited value.

This mirrors the Spartan root cause exactly: an unprivileged actor manipulates the shares/value ratio via a legitimate-looking but unrestricted function (`burn` in Spartan, `lending_pool_emissions_deposit` here) to make `P`(analogous to `total_asset_shares`) disproportionately small relative to the value denominator, causing new depositors' unit/share calculation to round to (near) zero.

### Impact Explanation
This is a concrete unauthorized value transfer / theft vector: a victim's deposited principal can be siphoned to the attacker via legitimate program instructions, with no admin privilege required. It directly maps to the "concrete theft/unauthorized transfer" acceptance criteria.

### Likelihood Explanation
Requires the attacker to control (or be the dominant depositor of) a bank with very low `total_asset_shares`, which is realistic for a newly created bank or a bank with very few, low-value depositors — a normal early-lifecycle state for any bank. The `lending_pool_emissions_deposit` function is explicitly permissionless and has no floor/step limits on how much `asset_share_value` can move in a single call besides overflow checks, and no minimum-shares-per-deposit protection is evident in the reviewed deposit path.

### Recommendation
- Add minimum-shares/anti-inflation protections to deposits (e.g., reject/round up deposits that would mint fewer than N shares, or use a virtual-shares/decimal-offset technique as in modern ERC-4626 mitigations).
- Restrict `lending_pool_emissions_deposit` (or its share-value-mutating effect) to a privileged emissions authority, or require it be proportional/limited relative to `total_asset_shares` so a single call cannot arbitrarily inflate `asset_share_value`.
- Consider seeding new banks with a minimum, unrecoverable "dead" share allocation similar to Uniswap V2's `MINIMUM_LIQUIDITY` to prevent low-`total_asset_shares` states from being exploitable.

### Proof of Concept
Not independently executed (index limitation); logic is derived from the cited source. Suggested reproduction with a Devin/test-harness session:
1. Create a bank and have `attacker` deposit a small amount (e.g., 1 unit) so `total_asset_shares` is tiny.
2. Have `attacker` call `lending_pool_emissions_deposit` with a very large `amount`, inflating `asset_share_value`.
3. Have `victim` call `lending_account_deposit` with a normal deposit amount; assert `victim`'s resulting `asset_shares` rounds to ~0 while their tokens were transferred into `liquidity_vault`.
4. Have `attacker` call `lending_account_withdraw` with `withdraw_all: true` and confirm they receive more value out than they put in (including the victim's stranded deposit), as tracked in `tests/specs/basic/18_emissionsDeposit.spec.ts` and `programs/marginfi/tests/misc/emissions_deposit.rs`, extended to a two-actor adversarial scenario.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L237-241)
```rust
    fn get_asset_amount(&self, shares: I80F48) -> MarginfiResult<I80F48> {
        Ok(shares
            .checked_mul(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/state/bank.rs (L249-256)
```rust
    fn get_asset_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        if self.asset_share_value == I80F48::ZERO.into() {
            return Ok(I80F48::ZERO);
        }
        Ok(value
            .checked_div(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L111-146)
```rust
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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1856)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
```
