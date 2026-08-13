Not out of scope. This finding is a valid analog. The `init_global_fee_state` instruction creates a program-wide singleton `FeeState` PDA via a permissionless `payer: Signer` account with no admin/authority check, and lets the caller pass an arbitrary `admin_key` argument that becomes `global_fee_admin`. This is architecturally identical to the `CsFeeOracle.initialize()` front-running bug: a one-time, unauthenticated initializer for a singleton privileged config account.

### Title
Permissionless, Front-Runnable Initialization of the Global `FeeState` Singleton Allows Attacker to Seize `global_fee_admin` - (File: `programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs`)

### Summary
The `init_global_fee_state` instruction (handler `initialize_fee_state`) initializes the program-wide singleton `FeeState` PDA (seeds `[FEE_STATE_SEED]`, i.e. a single canonical address per program) and directly sets `fee_state.global_fee_admin` and `fee_state.global_fee_wallet` from caller-supplied arguments [1](#0-0) . The associated `InitFeeState` accounts struct only requires `payer: Signer<'info>` — any signer — plus the `init` constraint on the PDA; there is no check that the caller is a designated deployer or program authority [2](#0-1) . The top-level dispatcher exposes it as callable by anyone with the comment "(Runs once per program)" but applies no access control [3](#0-2) .

### Finding Description
Because the `fee_state` account is a PDA derived from a fixed, publicly known seed (`FEE_STATE_SEED`), its address is deterministic and identical for every deployment of the program, and `Anchor`'s `init` constraint only requires that the account not already exist. Whoever's transaction lands first — not necessarily the legitimate protocol operator — becomes the permanent `global_fee_admin` (until subsequently changed by that admin) and sets `global_fee_wallet` to any address of their choosing. This is functionally identical to `CSFeeOracle.initialize()`: a critical, one-time, unauthenticated initializer for a privileged singleton, vulnerable to front-running on a public mempool/validator network.

The comment "Note: there is just one `FeeState` per program, so no further check is required" elsewhere in the codebase (e.g. `ConfigGroupFee`, `EditFeeState`, `PropagateFee`) confirms the developers rely on the PDA-uniqueness property for security of *subsequent* actions, but this property does nothing to protect the *initial* write, which is exactly where the privileged admin/wallet fields get set. Note: `init_global_fee_state_v2` (`FeeStateV2`) is separately permissionless but currently does not set any admin field [4](#0-3) , so it's not itself vulnerable to this exact issue, but its presence shows the general convention of no-owner-check for these "runs once" instructions.

### Impact Explanation
An attacker who successfully front-runs `init_global_fee_state` becomes `global_fee_admin`, which per the project's own documentation:
- Can edit global fee parameters (program fee rates, origination fee shares, init fees)
- Can change the global fee wallet (redirecting all protocol SOL/token fees to an attacker-controlled address)
- Can set/clear the pause delegate admin
- Can panic-pause the entire protocol [5](#0-4) 

This is unauthorized state change over a program-wide privileged account, with a path to theft of protocol fees (misdirected `global_fee_wallet`) and denial-of-service (unauthorized `panic_pause`) across every group in the deployment, not just a single unprivileged user's funds.

### Likelihood Explanation
The likelihood depends entirely on deployment operational security: if the legitimate deployer submits `init_global_fee_state` in the very same transaction/bundle as program deployment (as the test fixtures do, bundling group creation right after) the race window is minimal. However, the instruction itself provides no on-chain protection, so any deployment/upgrade flow that does not atomically bundle this call with deployment (e.g., a separate follow-up transaction, or on a network with public mempools) is exposed to front-running by any observer.

### Recommendation
Add access control to `InitFeeState`/`init_global_fee_state`, e.g.:
- Require the payer/signer to match a hardcoded program-upgrade-authority check (compare against the `BPFLoaderUpgradeable` program's authority for this program ID), or
- Gate initialization behind a constant/hardcoded expected admin pubkey compiled into the program, or
- Bundle the `init_global_fee_state` call atomically with program deployment/initialization tooling so no window exists for front-running, and add an explicit check that reverts if `admin_key` doesn't match an expected value.

### Proof of Concept
1. Attacker monitors for a fresh program deployment where `FeeState` PDA (`[FEE_STATE_SEED]`) does not yet exist.
2. Attacker (any funded signer) submits `init_global_fee_state(ctx, attacker_admin, attacker_wallet, ...)` before the legitimate operator's initialization transaction lands, exactly as shown in the test helper pattern used elsewhere in the repo [6](#0-5) .
3. `fee_state.global_fee_admin = attacker_admin` and `fee_state.global_fee_wallet = attacker_wallet` are now permanently set on the singleton PDA [7](#0-6) .
4. Attacker now controls `EditFeeState`/`ConfigGroupFee`/panic-pause calls, and all protocol fee flows destined for `global_fee_wallet` route to the attacker.

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

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state_v2.rs (L1-11)
```rust
use anchor_lang::prelude::*;
use marginfi_type_crate::{constants::FEE_STATE_V2_SEED, types::FeeStateV2};

/// Runs once per program to initialize the V2 fee state account.
pub fn initialize_fee_state_v2(ctx: Context<InitFeeStateV2>) -> Result<()> {
    let mut fee_state_v2 = ctx.accounts.fee_state_v2.load_init()?;
    fee_state_v2.key = ctx.accounts.fee_state_v2.key();
    fee_state_v2.bump_seed = ctx.bumps.fee_state_v2;

    Ok(())
}
```

**File:** guides/ADMIN/PERMISSIONS_AND_ROLES.md (L112-126)
```markdown
## Global Fee Admin

The `global_fee_admin` is separate from the group-level admin roles. It is stored on the `FeeState`
account (a global singleton).

**Can do:**
- Edit global fee parameters (program fee rates, origination fee shares, init fees)
- Change the global fee wallet
- Set or clear the dedicated pause delegate admin
- Panic-pause the entire protocol (with rate limiting: max 4 consecutive pauses, max 3 per day,
  each lasting 6 hours)

This role is intended for the protocol operator (e.g. the foundation) and controls protocol-level
economics and emergency pause functionality.

```

**File:** test-utils/src/marginfi_group.rs (L134-154)
```rust
                let init_fee_state_ix = Instruction {
                    program_id: marginfi::ID,
                    accounts: marginfi::accounts::InitFeeState {
                        payer: ctx.payer.pubkey(),
                        fee_state: fee_state_key,
                        system_program: system_program::id(),
                    }
                    .to_account_metas(Some(true)),
                    data: InitGlobalFeeState {
                        admin: ctx.payer.pubkey(),
                        fee_wallet: fee_wallet.pubkey(),
                        bank_init_flat_sol_fee: INIT_BANK_ORIGINATION_FEE_DEFAULT,
                        order_init_flat_sol_fee: ORDER_INIT_FLAT_FEE_DEFAULT,
                        liquidation_flat_sol_fee: LIQUIDATION_FLAT_FEE_DEFAULT,
                        program_fee_fixed: PROTOCOL_FEE_FIXED_DEFAULT.into(),
                        program_fee_rate: PROTOCOL_FEE_RATE_DEFAULT.into(),
                        liquidation_max_fee: LIQUIDATION_BONUS_FEE_MINIMUM.into(),
                        order_execution_max_fee: ORDER_EXECUTION_MAX_FEE.into(),
                    }
                    .data(),
                };
```
