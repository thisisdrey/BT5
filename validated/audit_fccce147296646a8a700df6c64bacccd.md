[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** tare-io__tare-contracts/specs/servicing.md (L7-19)
```markdown
### On-time payment

When a loan is paid on time, the following steps happen:

1. We calculate the interest, fees, and principal owed off-chain (or in the future with some other on-chain module)
2. The loan gets updated with the accrued but not yet paid amounts on-chain via `accrue`
3. Payments Provider deposits USDC into a pass-through account
4. The borrower (or admin) submits a transaction via `pay` that pulls funds from the `borrower` address into the Loans contract
5. Internal bookkeeping is updated with the payment allocation via `applyWaterfall`
6. Transactions are submitted to withdraw the respective amounts:
   - Investor or admin calls `investorWithdraw` to receive both principal and interest (as separate entries within one call)
   - Servicer calls `servicerWithdraw` to receive fees

```

**File:** tare-io__tare-contracts/specs/servicing.md (L87-95)
```markdown
Status transitions are managed by the servicer off-chain. The contract does not enforce a state machine — any status can be set to any other status via `updateLoanData`. The only enforced transitions are `Created → FullyFunded` (automatic when a valid full-commitment `fund()` call succeeds) and `FullyFunded → Active` (automatic during `disburse()`). Active servicing operations (`pay`, `accrue`, `chargeMiscFee`) require the loan to be in `Active` or `ChargedOff`. `applyWaterfall`, `returnFunds`, and `refundBorrower` additionally allow `FullyPaid` so residual payments, post-payoff corrections, and overpayment refunds can be processed after final payoff. Terminal statuses (`Cancelled`, `Closed`) also block `updateBorrower`, `updateServicer`, `updateLoanTerms`, and `fund`, but still allow with ... (truncated)

struct LoanData {
LoanStatus status;
uint48 updatedAt;
uint48 lastPaymentDate;
uint48 nextDueDate;
uint48 maturityDate;
}
```

**File:** tare-io__tare-contracts/SECURITY.md (L193-198)
```markdown
### 26 — Several state-changing functions remain callable on Cancelled / Closed loans

**Where:** [`Loans.refundBorrower`](contracts/Loans.sol), various `*Withdraw` paths in [`Loans.sol`](contracts/Loans.sol)

`applyWaterfall` will be gated `notCancelledOrClosed`, but `refundBorrower` and the withdraw functions intentionally remain callable on Cancelled / Closed loans to allow post-closure cleanup. Documented here so integrators don't assume terminal status freezes all state.

```

**File:** tare-io__tare-contracts/README.md (L1-10)
```markdown
# Tare Smart Contracts

Smart contracts, ABIs, and TypeScript bindings for the Tare protocol. Uses [Foundry](https://getfoundry.sh/) for smart contract development and [wagmi CLI](https://wagmi.sh/cli) for ABI/TypeScript codegen.

## Project Structure

- `contracts/` - Solidity smart contracts
- `src/` - TypeScript source (auto-generated ABIs, enums, deployment addresses)
- `test/` - Foundry test files
- `script/` - Solidity deployment scripts
```
