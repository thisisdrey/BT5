### Title
Unprotected one-time `init_global_fee_state` allows front-running to hijack the global fee admin and fee wallet - ([File: programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs])

### Summary
`init_global_fee_state` initializes the singleton `FeeState` PDA and is documented as "runs once per program," but the `InitFeeState` account context places no constraint on who may call it — any signer can be the `payer`, and the caller freely supplies `admin_key` and `fee_wallet` as instruction arguments. This mirrors the reported `L2EthToken::initialization` bug class: a critical, one-time setup call that anyone can win the race to call before the legitimate deployer/operator, permanently hijacking privileged state because the account uses Anchor's `init` constraint (which only prevents re-initialization, not unauthorized first initialization).

### Finding Description
The instruction handler and its accounts struct contain no signer allow-list, hardcoded pubkey check, or program-upgrade-authority check: [1](#0-0) [2](#0-1) 

The `payer` is only required to be a `Signer`, with no `has_one`, no constraint comparing it to a known deployer key, and the `admin_key`/`fee_wallet` values are taken verbatim from instruction arguments and written into the PDA on `load_init()`. Because the `fee_state` account is a deterministic PDA (`seeds = [FEE_STATE_SEED]`), whoever's transaction lands first "wins" — Anchor's `init` will simply fail for the legitimate operator's subsequent transaction with "account already in use," permanently locking in the attacker-supplied `global_fee_admin` and `global_fee_wallet`.

This is functionally identical to the reported bug class: an unprotected initializer for a singleton/critical config account that is supposed to be called exactly once by a trusted operator, but has no access control preventing any address from calling it first.

### Impact Explanation
Once an attacker wins the race and sets `fee_state.global_fee_admin` to their own key, they gain full control of `EditFeeState` (protected only by `has_one = global_fee_admin`): [3](#0-2) 

They can then set `fee_wallet` to an address they control. Every subsequent `marginfi_group_initialize` call copies `fee_state.global_fee_wallet` into each new group's cache: [4](#0-3) 

And `lending_pool_collect_bank_fees` (a permissionless instruction) validates the `fee_ata` against `fee_state.global_fee_wallet` and transfers accrued program fees to it: [5](#0-4) [6](#0-5) 

The net effect is a protocol-wide, permanent redirection of program fees (theft) and unauthorized control over global fee parameters (`edit_global_fee_state`), affecting every group/bank created under the program — a concrete unauthorized state change and theft vector in the core fee-accounting path.

### Likelihood Explanation
Exploitation requires only front-running the deployer's `init_global_fee_state` transaction — a single permissionless, unauthenticated call with a deterministic PDA address that is publicly derivable (`FEE_STATE_SEED`) before deployment. Any party monitoring the mempool/deployment sequence for a fresh program deployment (or a redeployment/migration scenario) could submit this transaction first. Likelihood is highest during initial deployment or any re-deployment workflow, and drops to zero once the PDA is successfully initialized by the legitimate operator (since it cannot be re-initialized). This is a real, if narrow-window, front-running risk rather than a routine attack surface, consistent with how the analogous zkSync issue was described as "acknowledged, not resolved."

### Recommendation
Add explicit access control to `InitFeeState`/`initialize_fee_state`, e.g.:
- Constrain `payer` to equal the program's upgrade authority (read via `bpf_loader_upgradeable` `ProgramData` account) or a hardcoded deployment-time constant address, similar to the TypeScript-templating approach suggested in the referenced report.
- Alternatively, bake the expected initial `admin_key`/`fee_wallet` into the program at build time rather than accepting them as attacker-controlled instruction arguments.
- Consider bundling `init_global_fee_state` atomically with program deployment/upgrade so no window exists for front-running.

### Proof of Concept
1. Program is deployed (or upgraded) on-chain; the `FeeState` PDA (`seeds = [b"feestate"]`) does not yet exist.
2. Attacker derives the same PDA address (publicly computable) and submits `init_global_fee_state(admin_key = attacker_pubkey, fee_wallet = attacker_wallet, ...)` before the legitimate deployer's setup transaction confirms.
3. Attacker's transaction lands first; `fee_state.global_fee_admin = attacker_pubkey` is now permanently set (Anchor's `init` prevents any later re-initialization).
4. Legitimate deployer's `init_global_fee_state` transaction fails with "account already in use."
5. Attacker calls `edit_global_fee_state` (authorized via `has_one = global_fee_admin`) to set `fee_wallet` to an address they control.
6. From this point on, every `lending_pool_collect_bank_fees` call across all groups/banks routes protocol program fees to the attacker's `fee_ata`, and every newly initialized `MarginfiGroup` inherits the attacker-controlled `global_fee_wallet` in its `fee_state_cache`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L1-35)
```rust
// Runs once per program to init the global fee state.
use anchor_lang::prelude::*;
use marginfi_type_crate::{
    constants::FEE_STATE_SEED,
    types::{FeeState, WrappedI80F48},
};

#[allow(unused_variables)]
pub fn initialize_fee_state(
    ctx: Context<InitFeeState>,
    admin_key: Pubkey,
    fee_wallet: Pubkey,
    bank_init_flat_sol_fee: u32,
    liquidation_flat_sol_fee: u32,
    order_init_flat_sol_fee: u32,
    program_fee_fixed: WrappedI80F48,
    program_fee_rate: WrappedI80F48,
    liquidation_max_fee: WrappedI80F48,
    order_execution_max_fee: WrappedI80F48,
) -> Result<()> {
    let mut fee_state = ctx.accounts.fee_state.load_init()?;
    fee_state.global_fee_admin = admin_key;
    fee_state.global_fee_wallet = fee_wallet;
    fee_state.key = ctx.accounts.fee_state.key();
    fee_state.bank_init_flat_sol_fee = bank_init_flat_sol_fee;
    fee_state.bump_seed = ctx.bumps.fee_state;
    fee_state.program_fee_fixed = program_fee_fixed;
    fee_state.program_fee_rate = program_fee_rate;
    fee_state.liquidation_max_fee = liquidation_max_fee;
    fee_state.liquidation_flat_sol_fee = liquidation_flat_sol_fee;
    fee_state.order_execution_max_fee = order_execution_max_fee;
    fee_state.order_init_flat_sol_fee = order_init_flat_sol_fee;

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L37-55)
```rust
#[derive(Accounts)]
pub struct InitFeeState<'info> {
    /// Pays the init fee
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        init,
        seeds = [
            FEE_STATE_SEED.as_bytes()
        ],
        bump,
        payer = payer,
        space = 8 + FeeState::LEN,
    )]
    pub fee_state: AccountLoader<'info, FeeState>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs (L108-121)
```rust
#[derive(Accounts)]
pub struct EditFeeState<'info> {
    /// Admin of the global FeeState
    pub global_fee_admin: Signer<'info>,

    // Note: there is just one FeeState per program, so no further check is required.
    #[account(
        mut,
        seeds = [FEE_STATE_SEED.as_bytes()],
        bump,
        has_one = global_fee_admin @ MarginfiError::Unauthorized
    )]
    pub fee_state: AccountLoader<'info, FeeState>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L30-33)
```rust
    marginfi_group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet;
    marginfi_group.fee_state_cache.program_fee_fixed = fee_state.program_fee_fixed;
    marginfi_group.fee_state_cache.program_fee_rate = fee_state.program_fee_rate;
    marginfi_group.banks = 0;
```

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L26-38)
```rust
    // Validate the program fee ata is correct
    {
        let mint = &bank.mint;
        let global_fee_wallet = &ctx.accounts.fee_state.load()?.global_fee_wallet;
        let token_program_id = &ctx.accounts.token_program.key();
        let program_fee_ata = &ctx.accounts.fee_ata.key();
        let ata_expected =
            get_associated_token_address_with_program_id(global_fee_wallet, mint, token_program_id);
        check!(
            program_fee_ata.eq(&ata_expected),
            MarginfiError::InvalidFeeAta
        );
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L127-163)
```rust
    // Transfer the program fee
    let (program_fee_transfer_amount, new_outstanding_program_fees) = {
        let outstanding = I80F48::from(bank.collected_program_fees_outstanding);
        let transfer_amount = min(outstanding, available_liquidity).int();

        (
            transfer_amount.int(),
            outstanding
                .checked_sub(transfer_amount)
                .ok_or_else(math_error!())?,
        )
    };

    available_liquidity = available_liquidity
        .checked_sub(program_fee_transfer_amount)
        .ok_or_else(math_error!())?;

    assert!(available_liquidity >= I80F48::ZERO);

    bank.collected_program_fees_outstanding = new_outstanding_program_fees.into();

    bank.withdraw_spl_transfer(
        program_fee_transfer_amount
            .checked_to_num()
            .ok_or_else(math_error!())?,
        liquidity_vault.to_account_info(),
        fee_ata.to_account_info(),
        liquidity_vault_authority.to_account_info(),
        maybe_bank_mint.as_ref(),
        token_program.to_account_info(),
        bank_signer!(
            BankVaultType::Liquidity,
            ctx.accounts.bank.key(),
            bank.liquidity_vault_authority_bump
        ),
        ctx.remaining_accounts,
    )?;
```
