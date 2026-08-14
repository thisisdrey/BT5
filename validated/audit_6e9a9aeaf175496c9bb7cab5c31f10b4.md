### Title
Front-Runnable One-Time `init_global_fee_state` Instruction Allows Unprivileged Attacker to Seize the Global Fee Admin Role - ([File: programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs])

### Summary
`init_global_fee_state` is documented as "runs once per program" and creates the singleton `FeeState` PDA that holds the protocol-wide `global_fee_admin`, `global_fee_wallet`, and all program fee parameters. The instruction is callable by any `Signer` with no restriction to a hardcoded deployer key, and the `admin_key` that becomes `global_fee_admin` is an arbitrary caller-supplied argument. This is the same bug class as the referenced `NoteERC20.initialize()` front-running finding: a permissionless, one-time initializer for a privileged singleton that is not atomically bound to deployment.

### Finding Description
`InitFeeState` requires only a generic `Signer<'info>` as `payer` and initializes the PDA at `seeds = [FEE_STATE_SEED]`: [1](#0-0) 

The handler sets `fee_state.global_fee_admin = admin_key` directly from the instruction argument, with no check that `payer` or `admin_key` corresponds to any known/expected deployer key: [2](#0-1) 

Because the `FeeState` PDA address is deterministic (`FEE_STATE_SEED` only, no per-caller seed) and the instruction is a plain permissionless `init`, exactly like the ERC1967 proxy pattern in the report where `initialize()` is not restricted to the deployer, an attacker monitoring the chain for the `marginfi` program deployment (or for the currently-un-initialized `FeeState` PDA on any freshly upgraded/redeployed program) can submit their own `init_global_fee_state` transaction first — before the legitimate team's setup transaction lands — passing their own pubkey as `admin_key`.

Once the PDA is initialized this way, all `global_fee_admin`-gated instructions become permanently controlled by the attacker, since these instructions only validate via `has_one = global_fee_admin`: [3](#0-2) [4](#0-3) 

This mirrors the report's root cause precisely: a privileged singleton's constructor-equivalent function is not restricted to the deployer and is not deployed+initialized atomically, enabling a race that lets an unprivileged party seize the privileged role.

### Impact Explanation
An attacker who wins this race becomes `global_fee_admin` and can:
- Call `edit_global_fee_state` to redirect `global_fee_wallet` to an attacker-controlled address, capturing all future protocol/program fees (`programFeeFixed`/`programFeeRate`, bank init fees, liquidation fees, order fees) — direct theft.
- Set arbitrary `pause_delegate_admin` and call `panic_pause`/`panic_unpause` to freeze or unfreeze the entire protocol at will (via `PanicPause`/`PanicUnpause` contexts gated the same way), causing a protocol-wide denial of service.
- Use `config_group_fee` to toggle program fees on/off for any group.

Because `global_fee_admin` can reassign itself via `edit_global_fee_state`, this is a permanent, unrecoverable privilege takeover (the legitimate team has no on-chain path back to control) unless the whole program is redeployed — directly comparable to the "unrecoverable gas expenses" / permanent loss of control described in the report.

### Likelihood Explanation
Exploitability depends entirely on the window between program deployment/upgrade and the team's `init_global_fee_state` transaction landing. This is a real but narrow deployment-time race: it requires the attacker to observe the program deployment and beat the team's initialization transaction (e.g., via mempool/slot monitoring and fee bidding), similar to the classic proxy-initializer front-running pattern. It is not exploitable against an already-initialized `FeeState` (the `init` constraint fails once the PDA exists), so it only threatens a brief deployment window, not steady-state operation.

### Recommendation
- Restrict `init_global_fee_state` to a hardcoded/known upgrade-authority key (e.g., require `payer` to match the program's upgrade authority via the `BPFLoaderUpgradeable` program data account), or
- Combine program deployment and `init_global_fee_state` into a single atomic transaction/deployment script, or
- Require `admin_key == payer.key()` and additionally require `payer` to equal a compile-time constant deployer pubkey, so a front-runner cannot self-assign as admin even if they win the race.

### Proof of Concept
1. Team deploys/upgrades the `marginfi` program and prepares (but has not yet sent) a transaction calling `init_global_fee_state(admin=team_multisig, wallet=team_wallet, ...)`.
2. Attacker monitors the network, observes the `FeeState` PDA for this program does not yet exist, and submits `init_global_fee_state(admin=attacker_pubkey, wallet=attacker_wallet, ...)` with a higher priority fee.
3. Attacker's transaction lands first; `FeeState.global_fee_admin` and `FeeState.global_fee_wallet` are now attacker-controlled.
4. Team's original transaction fails (`init` constraint: account already in use).
5. Attacker calls `edit_global_fee_state` to set fee rates/wallet as desired and/or `panic_pause`/`config_group_fee` to disrupt the protocol, using only the `has_one = global_fee_admin` check that now matches the attacker's key: [5](#0-4)

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L8-35)
```rust
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

**File:** programs/marginfi/src/instructions/marginfi_group/panic_unpause.rs (L39-52)
```rust
#[derive(Accounts)]
pub struct PanicUnpause<'info> {
    /// Global fee admin only.
    #[account(mut)]
    pub global_fee_admin: Signer<'info>,

    #[account(
        mut,
        seeds = [FEE_STATE_SEED.as_bytes()],
        bump,
        has_one = global_fee_admin @ MarginfiError::Unauthorized
    )]
    pub fee_state: AccountLoader<'info, FeeState>,
}
```
