### Title
Front-runnable global fee state initialization allows an attacker to seize `global_fee_admin` control - ([File: programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs])

### Summary
`init_global_fee_state` is a permissionless, one-time instruction that creates the singleton `FeeState` PDA and sets its `global_fee_admin` and `global_fee_wallet` fields directly from caller-supplied instruction arguments, with no check that the caller is the legitimate protocol deployer/governance. Whoever calls this instruction first (i.e., whoever wins the race after the program is deployed/upgraded) becomes the permanent global fee admin. This is the same bug class as the external report: the "deploy" (program bytecode goes live) and "initialize" (set owner/admin) steps are decoupled into two separate transactions, and an unprivileged attacker can front-run the legitimate initialization to seize privileged control.

### Finding Description
The `InitFeeState` accounts struct only requires a `payer: Signer` to fund rent for a PDA at the fixed seed `[FEE_STATE_SEED]`: [1](#0-0) 

The handler blindly writes attacker-controlled arguments (`admin_key`, `fee_wallet`) into the new `FeeState` account with no authorization check against any hardcoded/expected admin key: [2](#0-1) 

The public entrypoint documents this as "(Runs once per program)" but enforces no privilege — it is exposed exactly like any other permissionless instruction: [3](#0-2) 

Because the `fee_state` account uses `init` at a deterministic PDA (`seeds = [FEE_STATE_SEED.as_bytes()]`), only the *first* successful call can ever succeed — any subsequent attempt fails with an "already in use" error. This means the instruction is a race: whichever transaction lands first (attacker's or the deployer's) permanently sets `global_fee_admin` and `global_fee_wallet`. This mirrors the EnsoWalletFactory bug class precisely: bytecode deployment and admin-assignment are split into separate transactions with no atomic binding of "deployer" to "owner," so anyone observing the mempool/leader schedule can front-run the legitimate initialization call.

Once an attacker controls `global_fee_admin`, they gain access to `edit_global_fee_state`, which is gated only by `global_fee_admin`/`pause_delegate_admin` checks (per `guides/ADMIN/PERMISSIONS_AND_ROLES.md` and `edit_global_fee.rs`), allowing them to:
- Redirect `global_fee_wallet` (all protocol SOL/token fees) to an attacker-controlled wallet.
- Set arbitrary `program_fee_fixed` / `program_fee_rate` values that are cached into every `MarginfiGroup.fee_state_cache` on group initialization (`initialize_group` reads `fee_state.global_fee_wallet`, `program_fee_fixed`, `program_fee_rate` directly into the group state): [4](#0-3) 
- Pause the protocol via the panic/pause delegate mechanism controlled by the fee-state admin.

Since every `MarginfiGroup` (used across all pools — basic, staked, drift, kamino, solend, juplend, etc.) depends on this single global `FeeState` PDA, a successful front-run compromises fee routing and configuration protocol-wide, not just one isolated admin path.

### Impact Explanation
This is a concrete unauthorized state change / theft vector at the highest privilege tier of the protocol: an unprivileged attacker who wins the initialization race becomes the sole `global_fee_admin`, redirecting all protocol fee flows (`global_fee_wallet`) and controlling fee parameters cached into every group's accounting (`fee_state_cache`). This has direct financial impact (fee theft) and can be used to manipulate protocol economics or pause functionality across all markets built on marginfi.

### Likelihood Explanation
Likelihood depends entirely on operational practice: if the deploying team calls `init_global_fee_state` in the very same transaction/bundle as program deployment/upgrade (as some initializer scripts appear to attempt, e.g. bundling `init_fee_state_ix` with subsequent instructions in `test-utils/src/marginfi_group.rs`), the window is negligible. However, the on-chain instruction itself provides no protection — there is no check that the caller matches any expected pubkey, no timelock, and no atomicity guarantee enforced by the program. Any operational gap (e.g., deploying the program binary first and calling the initializer in a later, unbundled transaction, or during a re-deployment on a new address/testnet/devnet) creates a window where any public transaction submitter could front-run and claim `global_fee_admin`. This is a design flaw in the program itself, not merely an operational mistake, matching the report's recommendation to "combine deployment and initialization into a single transaction" or otherwise restrict who may call `initialize`.

### Recommendation
- Hardcode the expected `global_fee_admin` (or require a specific known governance/multisig pubkey) as a `constraint` on the `payer`/`admin` account in `InitFeeState`, rather than accepting an arbitrary `admin_key` argument.
- Alternatively, require the `payer` to itself sign as the intended `admin_key` (`constraint = payer.key() == admin_key`), so at minimum the caller cannot name someone else's arbitrary key while some other mitigation restricts who is allowed to call it (e.g., an `upgrade_authority` check via the program's `ProgramData` account, restricting the call to the program's upgrade authority).
- Ensure deployment tooling always initializes `FeeState` atomically with program deployment (single transaction / same deploy script), and treat any gap between deploy and init as a critical operational risk.

### Proof of Concept
1. Program `marginfi` is deployed (or upgraded) on-chain; `init_global_fee_state` has not yet been called, so the `FeeState` PDA at `[FEE_STATE_SEED]` does not exist yet.
2. Attacker monitors the network and, before the legitimate deployer's initialization transaction is confirmed, submits their own transaction calling:
   ```
   init_global_fee_state(
     admin = <attacker_pubkey>,
     fee_wallet = <attacker_wallet>,
     ... arbitrary fee parameters ...
   )
   ```
   using the `InitFeeState` accounts (`payer = attacker`, `fee_state = PDA[FEE_STATE_SEED]`, `system_program`) as shown in [1](#0-0) .
3. Because `fee_state` uses Anchor's `init` constraint, this transaction succeeds if it lands first, permanently setting `fee_state.global_fee_admin = attacker_pubkey` and `fee_state.global_fee_wallet = attacker_wallet` (see handler at [5](#0-4) ).
4. The legitimate deployer's subsequent `init_global_fee_state` call fails ("account already in use").
5. The attacker now calls `edit_global_fee_state` as the legal `global_fee_admin` to redirect fees or change protocol-wide fee parameters that get cached into every `MarginfiGroup` via `initialize_group` ( [6](#0-5) ).

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
