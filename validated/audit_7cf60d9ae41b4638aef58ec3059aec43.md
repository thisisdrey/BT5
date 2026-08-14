### Title
Front-runnable, unpermissioned `init_global_fee_state` allows an attacker to permanently seize the global fee-admin PDA - ([File: programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs])

### Summary
`init_global_fee_state` is documented as running "once per program" to create the singleton `FeeState` PDA that stores the protocol-wide `global_fee_admin` and `global_fee_wallet`. Like the Ramses `RamsesV3Factory::initialize` bug, this instruction has no access control tying it to the deploying team — any signer can call it first with attacker-controlled `admin_key`/`fee_wallet` parameters, permanently claiming the one-and-only `FeeState` PDA before the legitimate deployer does.

### Finding Description
`initialize_fee_state` writes the caller-supplied `admin_key` and `fee_wallet` directly into the `FeeState` account with no `has_one`, allowlist, or program-upgrade-authority check on `payer`: [1](#0-0) 

The `FeeState` account is a PDA derived only from the fixed seed `FEE_STATE_SEED` (no signer-derived component), so `init` can succeed exactly once, ever, per program deployment: [2](#0-1) 

This mirrors the Ramses V3 pattern exactly: a separately-deployed piece of state (`FeeState`, analogous to the deployer-factory link) is wired up via a permissionless `initialize`-style call that is meant to run once as part of the deployment sequence, but nothing enforces atomicity or restricts the caller. Any address watching the mempool/leader schedule for the program's deployment can front-run the marginfi team's own `init_global_fee_state` transaction.

Unlike the Ramses case — where redeploying the factory/deployer contracts is a viable (if annoying) fix — here the PDA is derived solely from a constant seed under the marginfi program ID, so a successful griefing/hijack transaction cannot be "undone" or reissued at a different address without either migrating to `init_global_fee_state_v2` (see below) or upgrading the program to change the seed, since `MarginfiGroupInitialize` and other flows read from this exact PDA: [3](#0-2) 

Note there is a V2 fee-state PDA (`init_global_fee_state_v2` / `FEE_STATE_V2_SEED`) with the identical unpermissioned pattern: [4](#0-3) [5](#0-4) 

Both are exposed program instructions with no privileged gate: [6](#0-5) 

### Impact Explanation
If exploited on a fresh/redeployed program, the attacker becomes `global_fee_admin` and sets `global_fee_wallet` to their own address inside the immutable-address `FeeState` PDA. From there, `edit_global_fee_state` (gated by `global_fee_admin`) and fee routing that reads `fee_state.global_fee_wallet`/cached fee values in every `MarginfiGroup` would be controlled by the attacker — this goes beyond mere griefing into a genuine unauthorized privilege/state takeover (attacker can subsequently redirect protocol fee flows, adjust fee parameters, and control the `pause_delegate_admin`). At minimum, the legitimate team's `init_global_fee_state` transaction reverts (the PDA already exists), permanently blocking the intended initialization path for that program ID and forcing operational disruption; at worst, it is a real, non-recoverable unauthorized takeover of a protocol-wide privileged role. This satisfies "unauthorized state change" and potential "theft" (redirected fees) criteria.

### Likelihood Explanation
Deployment/initialization transactions on Solana are public before/at confirmation, and PDA addresses (`FEE_STATE_SEED`) are deterministic and known ahead of time to anyone who has the program ID (which is public once deployed, even before the init transaction lands, e.g. via `solana program deploy` broadcasting or a leaked/observed program binary). Any user can submit a competing `init_global_fee_state` transaction with higher priority fee. Given the instruction is unconstrained, likelihood is high in a race scenario and only requires network-level front-running, not any special access.

### Recommendation
- Restrict `init_global_fee_state` (and `init_global_fee_state_v2`) to a known, hardcoded, or upgrade-authority-checked signer (e.g., `require!(ctx.accounts.payer.key() == UPGRADE_AUTHORITY || ...)`), or
- Make the instruction callable only once atomically as part of program deployment/migration tooling in the same transaction, avoiding any window where the PDA can be front-run, mirroring the "execute atomically" or "make permissioned" recommendation given for the Ramses bug.

### Proof of Concept
1. Program is deployed and `FEE_STATE_SEED` PDA address is deterministically computable by anyone from the public program ID.
2. Before the marginfi deployment team submits its official `init_global_fee_state(admin_key=teamAdmin, fee_wallet=teamWallet, ...)` transaction, an attacker submits their own `init_global_fee_state(admin_key=attacker, fee_wallet=attacker, ...)` transaction with a higher priority fee.
3. The attacker's transaction lands first; `fee_state.load_init()` succeeds and locks in `global_fee_admin = attacker`, `global_fee_wallet = attacker`, per [7](#0-6) .
4. The team's follow-up transaction fails with an "account already in use" error since the PDA (seeded only by `FEE_STATE_SEED`) already exists — the legitimate initialization is permanently blocked for this program deployment, and the attacker now holds the `global_fee_admin` role that gates `edit_global_fee_state`.

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

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L21-32)
```rust
    let fee_state = ctx.accounts.fee_state.load()?;

    // The fuzzer should ignore this because the "Clock" mock sysvar doesn't load until after the
    // group is init. Eventually we might fix the fuzzer to load the clock first...
    #[cfg(not(feature = "client"))]
    {
        let clock = Clock::get()?;
        marginfi_group.fee_state_cache.last_update = clock.unix_timestamp;
    }
    marginfi_group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet;
    marginfi_group.fee_state_cache.program_fee_fixed = fee_state.program_fee_fixed;
    marginfi_group.fee_state_cache.program_fee_rate = fee_state.program_fee_rate;
```

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state_v2.rs (L4-27)
```rust
/// Runs once per program to initialize the V2 fee state account.
pub fn initialize_fee_state_v2(ctx: Context<InitFeeStateV2>) -> Result<()> {
    let mut fee_state_v2 = ctx.accounts.fee_state_v2.load_init()?;
    fee_state_v2.key = ctx.accounts.fee_state_v2.key();
    fee_state_v2.bump_seed = ctx.bumps.fee_state_v2;

    Ok(())
}

#[derive(Accounts)]
pub struct InitFeeStateV2<'info> {
    /// Pays the init fee
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        init,
        seeds = [FEE_STATE_V2_SEED.as_bytes()],
        bump,
        payer = payer,
        space = 8 + FeeStateV2::LEN,
    )]
    pub fee_state_v2: AccountLoader<'info, FeeStateV2>,

```

**File:** programs/marginfi/src/lib.rs (L593-601)
```rust
    /// (Runs once per program) Initialize the V2 fee state PDA.
    pub fn init_global_fee_state_v2(ctx: Context<InitFeeStateV2>) -> MarginfiResult {
        marginfi_group::initialize_fee_state_v2(ctx)
    }

    /// (permissionless) Copy current FeeState values into FeeStateV2.
    pub fn copy_fee_state_to_v2(ctx: Context<CopyFeeStateToV2>) -> MarginfiResult {
        marginfi_group::copy_fee_state_to_v2(ctx)
    }
```
