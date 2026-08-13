### Title
Permissionless, un-owner-gated initialization of the global `FeeState` PDA can be frontrun to hijack the protocol's fee admin and fee wallet - (File: `programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs`)

### Summary
The `FeeState` account, which stores the program-wide `global_fee_admin` and `global_fee_wallet` used for all protocol fee collection, is initialized via `initialize_fee_state` against a PDA derived from a fixed, non-group-specific seed. The instruction has no constraint requiring `payer` to be a specific, pre-authorized address — any signer can call it and pass arbitrary `admin_key`/`fee_wallet` values. Because Anchor's `init` on the PDA succeeds only once, whoever calls this instruction first "wins" the singleton account, exactly mirroring the reported `Deployer`/`Factory` initialization-frontrunning bug class where ownership is granted to `msg.sender`/the first caller rather than the constructor.

### Finding Description
`initialize_fee_state` writes attacker-controlled values directly into the singleton `FeeState` account: [1](#0-0) 

The account is a PDA seeded only by `FEE_STATE_SEED` (no group key, no hardcoded authority), so it exists exactly once per program deployment: [2](#0-1) 

The `Accounts` struct requires only a generic `payer: Signer`, with no check that `payer` equals a known/expected deployer key or program-upgrade authority. This is the on-chain analog of the constructor/initialize pattern in the report: the account that should represent a fixed, trusted owner is instead assigned to whichever party calls the initialize instruction first. On a public network, an attacker monitoring the mempool/validator gossip for the legitimate `init_global_fee_state`/`init_global_fee_state_v2` transaction (or simply racing to call it before the real admin does, since nothing gates who may call it) can submit their own transaction first and become `global_fee_admin`, and set `global_fee_wallet` to an address they control.

Once set, `global_fee_admin` is the sole authority permitted to update fee parameters (`edit_global_fee.rs`, `config_group_fee.rs`) and the `global_fee_wallet` is the destination for protocol fees collected from every bank in every group via the fee-collection path (`collect_bank_fees.rs`, referenced by `FEE_STATE_SEED` usage across bank-fee and liquidation-fee instructions). An attacker who wins this race therefore gains lasting, unprivileged control over protocol-wide fee economics and fee proceeds across the entire deployment, not just a single group/account.

### Impact Explanation
If the attacker wins the initialization race, they become the permanent `global_fee_admin` and can redirect `global_fee_wallet` to their own address, causing all subsequently collected protocol/program fees (bank fees, liquidation fees, order-execution fees) from every marginfi group to flow to the attacker instead of the legitimate protocol treasury. This is concrete fee theft and an unauthorized state change to a core, program-wide accounting parameter, requiring a full redeploy (new PDA space cannot be reused once `init` succeeds) to remediate — imposing real financial and operational cost, matching the "Medium/Medium" severity profile of the referenced report.

### Likelihood Explanation
The instruction is permissionless by design (per the comment "Runs once per program to init the global fee state") and reachable by any signer with no elevated privileges required. The only mitigating factor is the narrow timing window (must be called before the legitimate deployment script), but this is precisely the frontrunning scenario the referenced report describes, and Solana's public mempool/leader-schedule visibility makes such races practically exploitable during deployment.

### Recommendation
Restrict `initialize_fee_state`/`init_global_fee_state` (and its `_v2` counterpart) so that only a hardcoded, trusted authority (e.g., the program's upgrade authority or a fixed multisig pubkey known at compile time) can execute it — for example, adding an Anchor `constraint = payer.key() == EXPECTED_DEPLOYER @ MarginfiError::Unauthorized` check, or gating the call behind a CPI from a governance/multisig program. Alternatively, bind admin assignment to the program's upgrade authority account (verified via `bpf_loader_upgradeable::UpgradeableLoaderState`) rather than trusting an arbitrary `admin_key` parameter supplied by the caller.

### Proof of Concept
1. Observe (or predict) that the legitimate deployer is about to submit a transaction calling `init_global_fee_state`/`init_global_fee_state_v2` with `FEE_STATE_SEED` PDA derivation.
2. Before the legitimate transaction lands, submit a competing transaction calling the same instruction with `payer = attacker`, `admin_key = attacker_pubkey`, `fee_wallet = attacker_controlled_wallet`.
3. Because `fee_state` is `init`'d with the deterministic `FEE_STATE_SEED` PDA, the attacker's transaction succeeds first and the account is now permanently owned/configured by the attacker; the legitimate deployer's subsequent transaction fails with an "account already in use" error.
4. The attacker, now `global_fee_admin`, can call `edit_global_fee`/`config_group_fee` to keep manipulating fee parameters, and all future `collect_bank_fees` calls across every marginfi group route protocol fees to the attacker's `fee_wallet`.

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
