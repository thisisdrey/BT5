### Permanent lock of loan cashflows upon removal from NAV - ([File: tare-io__tare-contracts/contracts/PortfolioVault.sol])

### Summary
Removing a loan from the `PortfolioVault` curated NAV list without first collecting its outstanding cashflows results in those funds becoming permanently unrecoverable. The vault's `collectCashflows` function explicitly reverts if a loan is not in the curated NAV set to prevent NAV manipulation. Consequently, if a loan is removed from the set (e.g., due to impairment or a self-healing re-sync), any principal or interest already repaid by the borrower but not yet "pulled" into the vault becomes trapped in the `Loans` contract.

### Finding Description
The `PortfolioVault` uses a curated list of loan IDs (`_navLoanIds`) to define which assets contribute to its NAV [1](#0-0) . Managers can explicitly remove loans via `removeLoansFromNav` [2](#0-1)  or `transferLoans` [3](#0-2) . Additionally, the `updateNav` function automatically removes loans from the list if the vault no longer owns the NFT [4](#0-3) .

The `collectCashflows` function is the only unprivileged path for the vault to retrieve repaid principal and interest from the `Loans` ledger [5](#0-4) . To prevent "excluded" loans from silently inflating the vault's NAV through `idleLiquidity`, this function requires every loan in the request to be present in the curated NAV set [6](#0-5) . 

If a loan is removed from the NAV set while it has a positive `investorPrincipalWithdrawable` or `investorInterestWithdrawable` balance in `Loans.sol`, those funds cannot be collected. Re-admitting the loan to the NAV set via `addLoansToNav` requires the vault to still own the NFT [7](#0-6) . If the NFT was transferred out or burned (e.g., in a charge-off scenario where the NFT is destroyed but cash was recovered), the cashflows are permanently locked.

### Impact Explanation
This leads to a permanent lock of USDC (the vault's asset) that belongs to the vault's shareholders. Even if the borrower has fully repaid the loan, the vault cannot claim the funds if the loan was removed from the NAV set. This matches the "Permanent or practically unrecoverable lock of USDC" impact gate.

### Likelihood Explanation
The likelihood is medium. It requires a specific sequence where a manager removes a loan (perhaps thinking it is distressed) or a loan is transferred out/self-healed before a final `collectCashflows` call is made. In institutional credit, loans are frequently modified or transferred, increasing the chance of this bookkeeping oversight.

### Recommendation
Modify `collectCashflows` to allow collection from any loan currently owned by the vault, regardless of its presence in the curated NAV set. Alternatively, ensure that `_removeLoanFromNav` and `transferLoans` internally trigger a `collectCashflows` call or check for zero withdrawable balances before proceeding.

### Proof of Concept
1. A loan in the vault's NAV set receives a repayment from the borrower. `Loans.investorWithdraw` now shows a positive balance for the vault.
2. The Portfolio Manager calls `vault.removeLoansFromNav([loanId])` to exclude the loan from valuation (e.g., due to an off-chain impairment).
3. The Manager later attempts to collect the repaid funds by calling `vault.collectCashflows([loanId], ref)`.
4. The call reverts with `LoanNotInNav()` [8](#0-7) .
5. If the loan NFT is subsequently transferred or the loan is closed, the USDC remains stuck in the `Loans` contract's `ACC_CASH` for that loan ID, with no way for the vault to retrieve it. [9](#0-8) .

### Citations

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L103-108)
```text
   * @dev Curated list of loan IDs included in NAV. Loans must be owned by the
   *      vault to count; ownership is re-verified on every nonce change. Donations
   *      landing in the vault are not added automatically and therefore cannot
   *      influence NAV until a manager explicitly admits them via `addLoansToNav`.
   */
  uint64[] internal _navLoanIds;
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L296-300)
```text
      } else {
        // Drop stale entry; swap-and-pop places a new entry at `cursor`, so do
        // not advance — the next iteration re-scans this slot.
        _removeLoanFromNav(loanId);
      }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L389-408)
```text
  function collectCashflows(
    uint64[] calldata loanIds,
    bytes32 ref
  ) external nonReentrant whenNotPaused returns (InvestorWithdrawalResult[] memory loanWithdrawals) {
    _requireManagerRole();
    _requireIdleNav();

    // Reject loans excluded from NAV; their cashflows would otherwise inflate NAV via idleLiquidity.
    uint256 length = loanIds.length;
    for (uint256 i; i < length; ++i) {
      require(_navLoanIndex[loanIds[i]] != 0, LoanNotInNav());
    }

    loanWithdrawals = loans.investorWithdraw(loanIds, uint48(block.timestamp), ref);

    // Mutates idleLiquidity and per-loan ledger state without bumping the ownership nonce.
    _invalidateNav();

    emit CashflowsCollected(loanWithdrawals);
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L443-443)
```text
      require(loansNFT.ownerOf(uint256(loanId)) == address(this), LoanNotOwned());
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L456-471)
```text
  function removeLoansFromNav(uint64[] calldata loanIds) external onlyRole(PORTFOLIO_MANAGER) whenNotPaused {
    _requireIdleNav();
    bool changed;
    uint256 length = loanIds.length;
    for (uint256 i; i < length; ++i) {
      uint64 loanId = loanIds[i];
      if (_navLoanIndex[loanId] != 0) {
        _removeLoanFromNav(loanId);
        changed = true;
      }
    }
    // Removing shrinks the valuation set without bumping the ownership nonce;
    // invalidate the cached NAV so approvals can't run against a snapshot
    // that still included these loans.
    if (changed) _invalidateNav();
  }
```

**File:** tare-io__tare-contracts/contracts/PortfolioVault.sol (L574-585)
```text
  function transferLoans(
    uint64[] calldata loanIds,
    address recipient
  ) external onlyRole(PORTFOLIO_MANAGER) nonReentrant whenNotPaused {
    _requireIdleNav();
    uint256 length = loanIds.length;
    for (uint256 i; i < length; ++i) {
      uint64 loanId = loanIds[i];
      _removeLoanFromNav(loanId);
      IERC721(address(loansNFT)).transferFrom(address(this), recipient, uint256(loanId));
    }
  }
```

**File:** tare-io__tare-contracts/contracts/Loans.sol (L852-896)
```text
  function investorWithdraw(
    uint64[] calldata loanIds,
    uint48 timestamp,
    bytes32 ref
  ) external whenNotPaused nonReentrant returns (InvestorWithdrawalResult[] memory results) {
    uint256 numLoans = loanIds.length;
    results = new InvestorWithdrawalResult[](numLoans);
    if (numLoans == 0) return results;

    ILoansNFT nft = loansNFT;
    uint64 currentLoanCount = loanCount;

    // Handle the first loan outside the loop so the investor/unlocker check
    // and caller authorization only happen once.
    uint64 firstLoanId = loanIds[0];
    require(firstLoanId != 0 && firstLoanId <= currentLoanCount, DoesNotExist());

    (address cachedInvestorAddress, address cachedUnlocker) = nft.ownerAndUnlocker(uint256(firstLoanId));
    address recipient;
    if (cachedUnlocker == address(0)) {
      _requireCallerOrAdmin(cachedInvestorAddress);
      recipient = cachedInvestorAddress;
    } else {
      require(cachedUnlocker == msg.sender, Unauthorized());
      recipient = msg.sender;
    }

    int128 totalTransfer = _processInvestorWithdrawal(firstLoanId, timestamp, ref, results, 0);

    for (uint256 i = 1; i < numLoans; ) {
      uint64 loanId = loanIds[i];
      require(loanId != 0 && loanId <= currentLoanCount, DoesNotExist());
      (address loanInvestor, address loanUnlocker) = nft.ownerAndUnlocker(uint256(loanId));
      require(loanInvestor == cachedInvestorAddress, Unauthorized());
      require(loanUnlocker == cachedUnlocker, Unauthorized());

      int128 transfer = _processInvestorWithdrawal(loanId, timestamp, ref, results, i);
      unchecked {
        totalTransfer += transfer;
        ++i;
      }
    }

    currency.safeTransfer(recipient, uint256(int256(totalTransfer)));
  }
```
