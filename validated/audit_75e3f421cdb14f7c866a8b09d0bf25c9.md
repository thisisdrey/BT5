## Title
USDC Blacklist/Freeze on `Loans.disburse` Permanently Strands Investor Principal With No Bypass Path - (File: `contracts/Loans.sol`)

### Summary
The external report's root cause is a single mandatory external token/messenger call embedded in a critical state-transition path, with no fallback or bypass if that call reverts, causing funds and downstream state to become permanently stuck. The same pattern exists in `Loans.disburse`: the function performs a mandatory `currency.safeTransfer(borrowers[loanId], netDisbursedAmount)` [1](#0-0)  as an unconditional step in the same transaction that settles the unfunded commitment and (in the outer `disburse` function) transitions the loan to `Active` and writes immutable loan terms. If the borrower address is on USDC's blacklist (or is blacklisted after loan creation but before disbursement), `safeTransfer` reverts, and there is no alternate/bypass code path — analogous to the Arbitrum OFT messenger case where there is no fallback to the canonical bridge once the preferred path is selected.

### Finding Description
`disburse` requires the caller (originator or admin) to supply `netDisbursedAmount`, checks that `netDisbursedAmount + originationFee` equals the outstanding `ACC_UNFUNDED_COMMITMENT`, verifies investor funding, and then calls the internal `_disburse` helper, which unconditionally calls `currency.safeTransfer(borrowers[loanId], uint256(int256(netDisbursedAmount)))` [2](#0-1) . This transfer is not guarded by try/catch, and no alternative recipient or deferred/pull-based disbursement path exists.

If `borrowers[loanId]` is on the USDC blacklist (or a similar freeze mechanism), `safeTransfer` reverts, which reverts the entire `disburse` call, including the ledger entries that settle `ACC_UNFUNDED_COMMITMENT` and the loan-status transition from `FullyFunded` to `Active`. The loan is now permanently stuck in `FullyFunded` with investor principal already pulled into the contract (`fund` already executed and irreversible without a matching counter-action) but unable to progress.

The only mitigation path identified in the codebase is `updateBorrower`, which requires the *new* borrower address to already be registered in the servicer's address book [3](#0-2) . If the servicer is unavailable or no alternate address is pre-registered, there is no way to move the funds — investor principal is permanently parked in `ACC_CASH` for that loan with no on-chain refund route, exactly mirroring the "no bypass, no canonical fallback" root cause of the external report.

This is not merely theoretical: it is explicitly documented as an already-identified code smell in the project's own AI audit report [3](#0-2) , described there as "USDC blacklist DoS at disburse."

### Impact Explanation
- Investor principal (`ACC_CASH` for the affected loan) becomes permanently locked with no unprivileged or even privileged on-chain recovery route unless a cooperative servicer proactively registers a fresh borrower address in advance.
- The loan cannot progress past `FullyFunded`: no waterfall, no payments, no investor withdrawal of principal, since disbursement (the state transition to `Active`) never completes.
- This matches the allowed impact class: "Permanent or practically unrecoverable lock of USDC ... caused by an unprivileged path" — the trigger (a third party, e.g., Circle, blacklisting the borrower address) is entirely outside the control of any Tare-privileged role, so it is not a "privileged-role abuse" exclusion; it is an external/adversarial-independent condition acting on an otherwise-honest borrower address.

### Likelihood Explanation
Requires the borrower's registered address to become blacklisted by the USDC issuer (Circle) at any point between `fund()` succeeding and `disburse()` being called — a scenario that is not attacker-controlled but is a realistic operational risk (e.g., regulatory/AML action, sanctions, or address compromise reported to Circle) and has already been flagged by the team's own AI-assisted audit as a known code smell, indicating it is considered plausible enough to note but not yet remediated in the contracts.

### Recommendation
- Decouple state transition from token transfer: settle the ledger entries and transition the loan to `Active` regardless of transfer outcome, then use a pull-based/claim pattern for the borrower disbursement (e.g., credit an internal claimable balance and let the borrower or an authorized redirect target withdraw later), or
- Wrap the `currency.safeTransfer` call in the borrower disbursement path with a fallback: if the transfer reverts, hold the funds in a recoverable escrow account tied to the loan (not simply reverting the whole transaction) and expose an explicit, permissionless-to-the-borrower (or admin-assisted) redirection function that does not require pre-registration by an online servicer.
- Loosen the `updateBorrower` precondition (or add a guardian/admin emergency path) so that recovery does not hard-depend on the servicer being online and having pre-registered a new address.

### Proof of Concept
1. Originator calls `create()` for a loan with `borrower = B`.
2. Investor calls `fund()`, pulling the full commitment into the contract (`ACC_CASH` increases, loan status → `FullyFunded`).
3. Before `disburse()` is called, Circle blacklists address `B` (or `B` was already blacklisted and this was not caught during `create`).
4. Originator calls `disburse(loanId, netDisbursedAmount, ...)`. Internally, `_disburse` calls `currency.safeTransfer(borrowers[loanId], netDisbursedAmount)` [4](#0-3) , which reverts because `B` is blacklisted by USDC.
5. The entire `disburse` transaction reverts; the loan is permanently stuck in `FullyFunded` with investor principal locked in the contract's `ACC_CASH` for that loan.
6. Recovery via `updateBorrower` requires a new borrower address already registered in the servicer's address book [3](#0-2) ; if unavailable, funds remain stuck with no on-chain refund mechanism.

**Caveat**: This exact scenario is already listed as a reviewed/acknowledged "code smell" in the project's `pashov-ai-audit-report-20260429.md` [3](#0-2) . It is not explicitly listed in the numbered "Known Security Issues and Trust Assumptions" section of `SECURITY.md` (no `disburse`/blacklist match found there), so its formal disposition (accepted risk vs. still-open finding) is unclear from the indexed content alone. Given the audit-scope instructions to reject issues already accepted in `SECURITY.md`, and given this is documented as reviewed in a separate audit artifact, this finding should be treated with reduced confidence as a "new" report — it may already be a known/accepted risk to the Tare team rather than a fresh discovery.

### Citations

**File:** tare-io__tare-contracts/contracts/Loans.sol (L436-471)
```text
  function _disburse(
    uint64 loanId,
    int128 netDisbursedAmount,
    int128 originationFee,
    uint48 timestamp,
    bytes32 ref
  ) internal returns (uint128 entryIndex) {
    // Entry 1: Withhold origination fee (OriginatorFeePayable -> UnfundedCommitment)
    // Creates originator fee liability, settles part of commitment
    if (originationFee > 0) {
      _createInternalEntry(
        loanId,
        ACC_ORIGINATOR_FEE_PAYABLE,
        ACC_UNFUNDED_COMMITMENT,
        originationFee,
        timestamp,
        ENTRY_ORIGINATOR_FEE_WITHHOLDING,
        ref
      );
    }

    // Entry 2: Disburse to borrower (Cash -> UnfundedCommitment)
    // Settles remaining commitment liability, decreases Cash
    entryIndex = _createInternalEntry(
      loanId,
      ACC_CASH,
      ACC_UNFUNDED_COMMITMENT,
      netDisbursedAmount,
      timestamp,
      ENTRY_DISBURSEMENT_TO_BORROWER,
      ref
    );

    // Transfer netDisbursedAmount to borrower
    currency.safeTransfer(borrowers[loanId], uint256(int256(netDisbursedAmount)));
  }
```

**File:** tare-io__tare-contracts/audits/pashov-ai-audit-report-20260429.md (L158-158)
```markdown
- **USDC blacklist DoS at disburse** — `Loans.disburse` — Code smells: `currency.safeTransfer(borrowers[loanId], netDisbursedAmount)` reverts if the borrower address is on USDC's blacklist (or becomes blacklisted post-create), and the only recovery path (`updateBorrower`) requires the new borrower to be registered in the servicer's address book — if the servicer is offline, investor principal is parked in `ACC_CASH` with no on-chain refund route.
```
