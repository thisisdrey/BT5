## Analysis

The reported bug class — a **singleton initializer with no access control that can be front-run**, forcing the legitimate deployer to lose control of a critical account — has a direct analog in marginfi-v2's `init_global_fee_state` instruction.

### Title
Unauthorized front-running of `init_global_fee_state` allows attacker to seize the global `FeeState` admin role - (File: `programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs`)

### Summary
`init_global_fee_state` initializes the program-wide singleton `FeeState` PDA account. The instruction's `InitFeeState` account context only requires a generic `payer: Signer<'info>` — there is no check restricting who may call it, and the `admin_key` value that becomes `fee_state.global_fee_admin` is taken directly from an attacker-controllable instruction argument.

### Finding Description
The `InitFeeState` context is defined with a `payer: Signer<'info>` and an `init`-constrained PDA derived solely from the fixed seed `FEE_STATE_SEED` (no admin/whitelist constraint): [1](#0-0) 

The handler blindly assigns the caller-supplied `admin_key` argument to `fee_state.global_fee_admin`, and sets `global_fee_wallet` from the caller-supplied `fee_wallet` argument, with no restriction on who invokes the instruction: [2](#0-1) 

Since the `FeeState` PDA has a deterministic address (`seeds = [FEE_STATE_SEED.as_bytes()]`) and Anchor's `init` constraint only requires the account not already exist, any unprivileged user can observe the deployer's transaction in the mempool/prior to it landing and submit their own `init_global_fee_state` call first with `admin_key` and `fee_wallet` set to attacker-controlled keys — this is the exact analog to the `PublicLock.initialize()` front-running bug class, where a permissionless, no-access-control initializer for a critical singleton config account can be claimed by an attacker before the legitimate deployer.

This is comparable to the `Unlock.sol` pattern: `setLocktemplate()` / `PublicLock.initialize()` had no restriction on the caller, allowing an attacker to seize control of a critical implementation/config object before the real owner.

### Impact Explanation
Whoever successfully calls `init_global_fee_state` first becomes the permanent `global_fee_admin` of the entire protocol's fee state, since downstream privileged instructions like `EditFeeState` gate on `has_one = global_fee_admin`: [3](#0-2) 

As the global fee admin, the attacker could set the `global_fee_wallet` to their own address (redirecting protocol fees), and use panic-pause functionality (`panic_pause`/`panic_unpause`), which is also gated on the fee-state admin. Because `FEE_STATE_SEED` is a fixed, deterministic PDA seed with no additional constraints, the account cannot simply be re-derived elsewhere — the legitimate team would need to redeploy the program with a different program ID (or a different seed) to get an uncontested `FeeState` PDA, mirroring the "unnecessary redeployment/loss of funds" impact described in the referenced report.

### Likelihood Explanation
This requires no special permissions — any wallet capable of submitting a transaction can call `init_global_fee_state`, and since the `FeeState` PDA address is fully deterministic from the program ID and a public constant seed, an attacker can pre-compute it and race the deployer's initial setup transaction (e.g., during program upgrade/redeploy events, since it's a "runs once" instruction per the code comment). No secret information or privileged authority is needed to win the race.

### Recommendation
Restrict `init_global_fee_state` to only be callable by a hardcoded/trusted authority (e.g., the program's upgrade authority checked via `#[account(constraint = ...)]`, or a multisig pubkey baked into the program), rather than accepting an arbitrary `admin_key` parameter from any signer. Alternatively, perform this initialization atomically as part of program deployment/migration tooling in the same transaction as deployment so there is no window for front-running.

### Proof of Concept
1. Observe (or predict) that the legitimate admin is about to call `init_global_fee_state` (deterministic PDA derived only from `FEE_STATE_SEED`).
2. Submit a transaction calling `init_global_fee_state(ctx, attacker_pubkey, attacker_fee_wallet, ...)` with any funded `payer` signer before the legitimate transaction lands.
3. The `FeeState` account initializes with `global_fee_admin = attacker_pubkey` and `global_fee_wallet = attacker_fee_wallet`.
4. The legitimate deployer's subsequent `init_global_fee_state` call fails since the account already exists (`init` constraint), permanently locking the attacker in as `global_fee_admin`, per code at: [4](#0-3)

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L9-35)
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
