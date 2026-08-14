### Title
Front-runnable, unauthenticated `initialize_fee_state` allows an attacker to seize the global fee admin role - (File: `programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs`)

### Summary
The global `FeeState` singleton account is created via `initialize_fee_state`, an instruction that anyone can invoke, with no check restricting the caller to a deployer or hardcoded authority. The caller freely supplies the `admin_key` that becomes the permanent `global_fee_admin`.

### Finding Description
`InitFeeState` only requires a generic `payer: Signer` and initializes a PDA derived solely from the fixed seed `FEE_STATE_SEED` (i.e., a program-wide singleton, not scoped to any group or caller identity): [1](#0-0) 

The handler sets `fee_state.global_fee_admin = admin_key` and `fee_state.global_fee_wallet = fee_wallet` directly from caller-supplied arguments, with no signer/authority check verifying that `payer` (or anyone) is the legitimate protocol deployer: [2](#0-1) 

This is exactly the bug class in the external report: an `init` function for a singleton configuration account that anyone can call, creating a race between the legitimate deployer and an attacker to claim a privileged role. Because the `fee_state` PDA has no per-caller seed component, only the first successful call succeeds program-wide; every subsequent call fails since the account already exists. This differs from `MarginfiGroupInitialize`, which is safe because each group is a fresh, caller-supplied `Keypair` account (not a fixed-seed singleton), so "racing" it doesn't let an attacker hijack anyone else's group — it only lets them create their own group, which is the intended permissionless behavior.

### Impact Explanation
If an attacker front-runs the legitimate deployment transaction and calls `initialize_fee_state` first, they become the `global_fee_admin` and control `global_fee_wallet`, `program_fee_fixed`, `program_fee_rate`, `liquidation_flat_sol_fee`, `liquidation_max_fee`, `order_init_flat_sol_fee`, and `order_execution_max_fee` for the entire protocol. These values are later pulled into every marginfi group's cache via `initialize_group`/`propagate_fee_state`: [3](#0-2) 

and the global fee admin also has permissionless-relative-to-groups power via `config_group_fee` to toggle program fees on any group without that group admin's involvement: [4](#0-3) 

An attacker who captures this role could redirect protocol fee flows to their own wallet or set maximal fee parameters that are then propagated into every bank/group across the deployment, resulting in unauthorized state change and diversion of protocol fee revenue.

### Likelihood Explanation
Low but non-zero, matching the judge's original C4 assessment: the attacker must win a one-time race at deployment before the legitimate multisig/admin executes `initialize_fee_state`. This requires mempool/slot-level timing precision on Solana, and the deployment team would need to fail to notice the account was already initialized (an `init` constraint would simply fail their own transaction, immediately signaling the attack). Impact is protocol-wide but the attack window is narrow and singular.

### Recommendation
Restrict `InitFeeState` to a known, hardcoded/upgrade authority — e.g., require `payer` to match the program's upgrade authority (via `bpf_loader_upgradeable::UpgradeableLoaderState::ProgramData`) or gate the call behind a constant deployer pubkey compiled into the program, consistent with the original report's recommendation to add a deployer check to `init`-style functions.

### Proof of Concept
1. An attacker monitors the mempool/program deployment of `marginfi`.
2. Before the legitimate team's deployment script sends its `initialize_fee_state` transaction, the attacker submits their own transaction calling `initialize_fee_state(admin_key = attacker_key, fee_wallet = attacker_wallet, ...)` with the correctly derived `FEE_STATE_SEED` PDA as `fee_state` and themselves as `payer`.
3. Because `InitFeeState` has no authority check [1](#0-0) , the attacker's transaction succeeds first, permanently setting `fee_state.global_fee_admin = attacker_key` and `fee_state.global_fee_wallet = attacker_wallet`.
4. The legitimate deployment's subsequent `initialize_fee_state` call fails (`init` constraint: account already in use).
5. The attacker now controls global fee routing/config for every marginfi group created going forward, via `config_group_fee` and the cached values pulled during `marginfi_group_initialize`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L9-24)
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

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L21-33)
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
    marginfi_group.banks = 0;
```

**File:** programs/marginfi/src/lib.rs (L637-645)
```rust
    /// (global fee admin only) Enable or disable program fees for any group. Does not require the
    /// group admin to sign: the global fee state admin can turn program fees on or off for any
    /// group
    pub fn config_group_fee(
        ctx: Context<ConfigGroupFee>,
        enable_program_fee: bool,
    ) -> MarginfiResult {
        marginfi_group::config_group_fee(ctx, enable_program_fee)
    }
```
