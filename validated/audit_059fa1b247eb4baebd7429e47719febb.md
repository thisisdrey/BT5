### Title
`init_global_fee_state` allows front-running of the `global_fee_admin` and `global_fee_wallet` singleton - ([File: programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs])

### Summary
The `PrizePool.constructor`/`setDrawManager` bug class — a privileged role assigned via an instruction that anyone can call, protected only by an Anchor "init"/"runs once" guard rather than by actual signer/authority validation — has a direct analog in marginfi's global `FeeState` singleton initialization.

### Finding Description
`initialize_fee_state` (invoked via `init_global_fee_state` in `programs/marginfi/src/lib.rs`) creates the program-wide `FeeState` PDA and sets `global_fee_admin` and `global_fee_wallet` directly from caller-supplied arguments: [1](#0-0) 

The corresponding `InitFeeState` accounts struct only requires a generic fee-paying `Signer`, with no constraint verifying that the caller is the program's upgrade authority, deployer, or any other privileged identity: [2](#0-1) 

The only protection against a malicious value being set is the Anchor `init` constraint on the PDA (seeded solely by `FEE_STATE_SEED`), which prevents the instruction from being called a second time — exactly analogous to the PoolTogether `drawManager` pattern where `if (drawManager != address(0)) revert` is the only gate, rather than checking `msg.sender`/an authorized deployer. This is confirmed by the lib.rs doc comment describing it as merely "(Runs once per program)": [3](#0-2) 

Once initialized, all future control over the singleton (fee rates, `global_fee_wallet`, `pause_delegate_admin`) is gated behind `global_fee_admin` via `edit_global_fee_state`: [4](#0-3) 

Because the initializing instruction itself has no access control, any unprivileged actor who submits `init_global_fee_state` before the legitimate deployer/admin does (front-running at deploy time) permanently becomes `global_fee_admin` and sets `global_fee_wallet` to an address they control — identical in structure to the referenced report's root cause ("anyone can call ... and set a malicious address ... once set, it can not be updated").

### Impact Explanation
If an attacker front-runs this one-time initialization:
- They become the permanent `global_fee_admin`, with unilateral power to edit protocol-wide fee parameters (`program_fee_fixed`, `program_fee_rate`, `liquidation_max_fee`, `order_execution_max_fee`, etc.) and to set/clear `pause_delegate_admin`, per `edit_fee_state`.
- They can redirect `global_fee_wallet` to their own address, causing all subsequent program-level fees (init fees, liquidation fees, order fees, Drift bad-debt sweeps, etc.) to be misappropriated indefinitely, since legitimate governance no longer holds `global_fee_admin` and cannot self-correct without a program redeploy.
This matches the "unauthorized transfer" / "unauthorized state change" impact bar since it is a protocol-wide, permanent takeover of a privileged role and its associated fee flows.

### Likelihood Explanation
This requires the attacker to win a race at the moment `init_global_fee_state` is first submitted on-chain (deploy time), which is a narrow but real window on Solana if the initialization transaction is broadcast separately from program deployment/upgrade and is publicly observable in the mempool before confirmation. It is not exploitable after initialization succeeds legitimately, since the PDA `init` constraint prevents re-initialization. I was unable to verify from the available code/tests whether the operational deployment tooling (e.g., in `p0-cli`) always bundles this call atomically with program deployment in a way that eliminates the front-running window — this remains uncertain and would need to be checked in the actual deployment runbooks/scripts.

### Recommendation
Add an explicit authority check to `InitFeeState` (e.g., require the signer to match a hard-coded/expected deployer key, or require the program's upgrade authority via the `bpf_loader_upgradeable` program account), rather than relying solely on the PDA `init` uniqueness constraint to gate this privileged action.

### Proof of Concept
Conceptual PoC (cannot be executed without deployment tooling access):
1. Attacker monitors for the marginfi program's deployment/upgrade transaction.
2. Immediately after the program account becomes executable but before the legitimate team submits `init_global_fee_state`, the attacker submits their own `init_global_fee_state(admin_key=attacker, fee_wallet=attacker_ata, ...)` transaction.
3. The PDA `FeeState` (seeded only by `FEE_STATE_SEED`) is created with the attacker as `global_fee_admin` and `attacker_ata` as `global_fee_wallet`.
4. The legitimate team's subsequent `init_global_fee_state` call fails (`init` constraint — account already exists), and they have no way to call `edit_global_fee_state` since that requires the (now attacker-controlled) `global_fee_admin` signer.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L9-34)
```rust
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

**File:** programs/marginfi/src/lib.rs (L565-591)
```rust
    /// (Runs once per program) Configures the fee state account, where the global admin sets fees
    /// that are assessed to the protocol
    pub fn init_global_fee_state(
        ctx: Context<InitFeeState>,
        admin: Pubkey,
        fee_wallet: Pubkey,
        bank_init_flat_sol_fee: u32,
        liquidation_flat_sol_fee: u32,
        order_init_flat_sol_fee: u32,
        program_fee_fixed: WrappedI80F48,
        program_fee_rate: WrappedI80F48,
        liquidation_max_fee: WrappedI80F48,
        order_execution_max_fee: WrappedI80F48,
    ) -> MarginfiResult {
        marginfi_group::initialize_fee_state(
            ctx,
            admin,
            fee_wallet,
            bank_init_flat_sol_fee,
            liquidation_flat_sol_fee,
            order_init_flat_sol_fee,
            program_fee_fixed,
            program_fee_rate,
            liquidation_max_fee,
            order_execution_max_fee,
        )
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs (L10-31)
```rust
pub fn edit_fee_state(
    ctx: Context<EditFeeState>,
    admin: Option<Pubkey>,
    fee_wallet: Option<Pubkey>,
    bank_init_flat_sol_fee: Option<u32>,
    liquidation_flat_sol_fee: Option<u32>,
    order_init_flat_sol_fee: Option<u32>,
    program_fee_fixed: Option<WrappedI80F48>,
    program_fee_rate: Option<WrappedI80F48>,
    liquidation_max_fee: Option<WrappedI80F48>,
    order_execution_max_fee: Option<WrappedI80F48>,
    pause_delegate_admin: Option<Pubkey>,
) -> Result<()> {
    let mut fee_state = ctx.accounts.fee_state.load_mut()?;
    if let Some(admin) = admin {
        msg!(
            "Updating global_fee_admin: {:?} -> {:?}",
            fee_state.global_fee_admin,
            admin
        );
        fee_state.global_fee_admin = admin;
    }
```
