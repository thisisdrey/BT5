## Analysis

The external report concerns hardcoded fee/royalty percentages with no admin-controlled update mechanism. Searching the marginfi-v2 codebase for an analogous pattern, most fee parameters (interest rate fees, origination fee, program fees) **are** admin-configurable per bank via `configure_bank`/`InterestRateConfigOpt`, and global fees are configurable via `FeeState` fields like `program_fee_rate`, `liquidation_max_fee`, etc.

However, the **classic liquidation fee split** used in `lending_account_liquidate` is a literal hardcoded constant with no update path at all — not even an admin-gated one.

### Title
Hardcoded classic liquidation fee constants (`LIQUIDATION_LIQUIDATOR_FEE`/`LIQUIDATION_INSURANCE_FEE`) cannot be adjusted, risking uncollateralized bad debt during high-volatility liquidations - (File: type-crate/src/constants.rs)

### Summary
`LIQUIDATION_LIQUIDATOR_FEE` and `LIQUIDATION_INSURANCE_FEE` are hardcoded to 2.5% each in `type-crate/src/constants.rs`, with an explicit `TODO: Make these variable per bank` comment acknowledging the limitation. Unlike every other fee/rate parameter in the protocol (interest rate fees, origination fees, program fees, and even the newer receivership liquidation's `liquidation_max_fee`), these constants have zero update path — not via governance, not via the group admin, not via the global fee admin.

### Finding Description
`lending_account_liquidate` computes the liquidator's discount and insurance-fund cut directly from these two fixed constants: [1](#0-0) [2](#0-1) 

Every other economically-sensitive rate in the protocol was made admin-configurable specifically to allow tuning for market conditions — interest fees via `InterestRateConfigOpt` in `lending_pool_configure_bank`, and even the newer receivership liquidation path's max fee via `FeeState.liquidation_max_fee`. The classic liquidation discount, by contrast, is baked into the binary at compile time for every bank and every asset, regardless of volatility, liquidity depth, or risk tier.

This is exactly the bug class in the external report: a percentage that materially affects protocol solvency is hardcoded and cannot be updated by any owner/admin mechanism, even though the surrounding code clearly anticipated (via the TODO) that it should be configurable per bank.

### Impact Explanation
A flat 5% total discount (2.5% liquidator profit + 2.5% insurance) is a fixed incentive regardless of the volatility or liquidity of the specific collateral/liability pair being liquidated. During periods of high volatility, thin liquidity, or fast price crashes, a 2.5% liquidator profit margin may be insufficient to compensate for slippage risk and price movement between transaction submission and execution — third-party liquidators are economically encouraged to skip liquidating fast-moving or thin-liquidity positions, causing under-collateralized positions to remain open longer and increasing bank insolvency/bad-debt risk that ultimately socializes losses onto depositors and the insurance fund. Since the group/global admins have no lever to raise the liquidator incentive for a specific bank in response to changing market conditions (unlike every other rate in the system), this is a structural, protocol-wide risk rather than a theoretical one, and it directly affects the core accounting/liquidation path.

### Likelihood Explanation
This does not require any privileged action or governance attack — it is a passive design gap that manifests under normal, permissionless market conditions (any third-party liquidator can be discouraged from liquidating a volatile/illiquid bank at any time). It is highly likely to matter for newly listed, thinly-traded, or highly volatile collateral types where a static 2.5% fee is a known-insufficient economic incentive, and the codebase's own `TODO` comment indicates the team already recognized this design gap needs remediation.

### Recommendation
Move `LIQUIDATION_LIQUIDATOR_FEE` and `LIQUIDATION_INSURANCE_FEE` from global constants into per-bank configurable fields (consistent with the existing `InterestRateConfigOpt`/`BankConfigOpt` pattern), gated by the group admin via the existing `lending_pool_configure_bank` instruction, with sane bounds enforced on-chain (as is already done for `liquidation_max_fee` in `FeeState`).

### Proof of Concept
No exploit is required to demonstrate the flaw — it is directly visible in the source: [1](#0-0) 
This constant is used unconditionally for every bank in every classic liquidation call: [3](#0-2) 
There is no instruction, admin authority check, or account field anywhere in the program that permits updating these two constants; a full repository search for `LIQUIDATION_LIQUIDATOR_FEE`/`LIQUIDATION_INSURANCE_FEE` returns only their definition, their one usage site in `liquidate.rs`, and documentation describing them as fixed values, confirming no update mechanism exists.

### Citations

**File:** type-crate/src/constants.rs (L29-31)
```rust
/// TODO: Make these variable per bank
pub const LIQUIDATION_LIQUIDATOR_FEE: I80F48 = I80F48!(0.025);
pub const LIQUIDATION_INSURANCE_FEE: I80F48 = I80F48!(0.025);
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate.rs (L237-239)
```rust
        let final_discount: I80F48 =
            I80F48::ONE - (LIQUIDATION_INSURANCE_FEE + LIQUIDATION_LIQUIDATOR_FEE);
        let liquidator_discount: I80F48 = I80F48::ONE - LIQUIDATION_LIQUIDATOR_FEE;
```
