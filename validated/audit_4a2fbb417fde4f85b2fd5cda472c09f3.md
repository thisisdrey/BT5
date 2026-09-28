### Title
`DebtToken.repay` can be front-run by a dust repayment, making fixed-size full repayments revert - ([File: `contracts/DebtToken.sol`](contracts/DebtToken.sol))

### Summary
`DebtToken.repay` permits any payer to repay debt for an arbitrary `onBehalfOf_` account, because the payer is `_msgSender()` while the debt beneficiary is supplied separately. [1](#0-0)  An attacker can front-run a borrower’s fixed-size full repayment with a small successful repayment, reducing the borrower’s debt before the borrower’s transaction executes. [2](#0-1)  The borrower’s stale `_repaid` amount then exceeds the remaining debt and the transaction reverts in `_burn`. [3](#0-2) 

### Finding Description
`repay` converts the caller-supplied gross `amount_` into `_repaid` using `quoteRepayOut`, optionally collects the repay fee from the payer, burns `_repaid` synthetic tokens from the payer, and then burns `_repaid` debt from `onBehalfOf_`. [4](#0-3) 

No modifier restricts the payer to the borrower, so an attacker holding even a small amount of the synthetic asset can call `repay(victim, dustAmount)` directly. [2](#0-1)  With zero repay fee, `quoteRepayOut` returns the input amount unchanged, so even a one-wei repayment reduces the victim’s debt. [5](#0-4) 

For example, a victim with debt `D` submits `repay(victim, D)` when the repay fee is zero. The attacker front-runs it with `repay(victim, dust)`, leaving `D - dust`. The victim’s transaction still calculates `_repaid = D`, and `_burn(victim, D)` reverts because the current balance is lower than the requested burn amount. [6](#0-5) 

If `debtFloorInUsd` is nonzero, the same stale amount can revert even earlier because `balanceOf(onBehalfOf_) - _repaid` underflows before the debt-floor quote is made. [7](#0-6) 

### Impact Explanation
An unprivileged attacker can repeatedly force fixed-size repayment transactions to revert by spending only enough synthetic tokens to reduce the target debt below the already-calculated `_repaid` amount. [8](#0-7) 

This temporarily prevents a borrower from executing the submitted full repayment and delays the debt reduction required to unlock collateral or complete a bundled withdrawal workflow. The borrower can submit a newly calculated amount afterward, but the attacker can repeat the same ordering attack against subsequent fixed-size transactions.

### Likelihood Explanation
The attack only requires the attacker to hold a small positive balance of the relevant synthetic token and to front-run a publicly visible repayment transaction. `repay` is externally callable while the pool is not shutdown, and its checks do not require the payer to equal `onBehalfOf_`. [2](#0-1) 

The attack is most reliable against transactions that encode a quoted full-repayment amount rather than using `repayAll`, because `repay` does not clamp the requested repayment to the current debt balance. [4](#0-3) 

### Recommendation
Calculate the current beneficiary debt inside `repay` and cap the consumed gross amount to the amount required to repay that debt before calculating fees.

Conceptually:

```solidity
uint256 currentDebt = balanceOf(onBehalfOf_);
(uint256 requiredGrossAmount, ) = quoteRepayIn(currentDebt);
uint256 amountToUse = Math.min(amount_, requiredGrossAmount);

(_repaid, _fee) = quoteRepayOut(amountToUse);
```

Then collect `_fee`, burn `_repaid`, and reduce debt by `_repaid` based on `amountToUse` rather than the stale caller-provided total. This preserves third-party repayments while making repayment idempotent when the debt decreases between transaction construction and execution. [4](#0-3) 

Alternatively, restrict `repay` so the payer can only repay their own debt, although that would remove the existing third-party repayment functionality. [1](#0-0) 

### Proof of Concept
The following Hardhat regression test can be inserted inside the existing `when some synth was issued` context, where `user1` already has 100 msUSD of debt and 100 msUSD. [9](#0-8) 

```ts
it('reverts a full repayment after a dust repayment front-run', async function () {
  const dust = parseEther('1')

  // The attacker obtains synthetic tokens through the normal public issue path.
  await met.mint(user2.address, parseEther('1000'))
  await met.connect(user2).approve(msdMET.address, MaxUint256)
  await msdMET.connect(user2).deposit(parseEther('1000'), user2.address)
  await msUSDDebt.connect(user2).issue(dust, user2.address)

  // Attacker front-runs user1 and repays 1 msUSD on their behalf.
  await msUSDDebt.connect(user2).repay(user1.address, dust)
  expect(await msUSDDebt.balanceOf(user1.address)).eq(amount.sub(dust))

  // user1's already-submitted full repayment now requests 100 debt
  // to be burned while only 99 remains.
  await expect(
    msUSDDebt.connect(user1).repay(user1.address, amount)
  ).revertedWithCustomError(msUSDDebt, 'BurnAmountExceedsBalance')
})
```

The attacker’s call succeeds because `repay` intentionally treats the caller as payer and `onBehalfOf_` as the account whose debt is reduced. [10](#0-9)  The victim’s call then reaches `_burn` with `_accountBalance < amount_` and reverts. [3](#0-2)

### Citations

**File:** contracts/DebtToken.sol (L401-408)
```text
    function quoteRepayOut(uint256 amount_) public view override returns (uint256 _amountToRepay, uint256 _fee) {
        uint256 _repayFee = pool.feeProvider().repayFee();
        if (_repayFee == 0) {
            return (amount_, _fee);
        }

        _amountToRepay = amount_.wadDiv(HUNDRED_PERCENT + _repayFee);
        _fee = amount_ - _amountToRepay;
```

**File:** contracts/DebtToken.sol (L412-427)
```text
     * @notice Send synthetic token to decrease debt
     * @dev The _msgSender() is the payer and the account beneficed
     * @param onBehalfOf_ The account that will have debt decreased
     * @param amount_ The amount of synthetic token to burn (this is the gross amount, the repay fee will be subtracted from it)
     * @return _repaid The amount repaid after fees
     */
    function repay(
        address onBehalfOf_,
        uint256 amount_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
```

**File:** contracts/DebtToken.sol (L431-454)
```text
        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (_repaid, _fee) = quoteRepayOut(amount_);
        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, _pool.feeCollector(), _fee);
        }

        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L525-535)
```text
    function _burn(address account_, uint256 amount_) private updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert BurnFromNullAddress();

        uint256 _accountBalance = balanceOf(account_);
        if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

        unchecked {
            principalOf[account_] = _accountBalance - amount_;
            debtIndexOf[account_] = debtIndex;
            totalSupply_ -= amount_;
        }
```

**File:** test/DebtToken.test.ts (L601-610)
```typescript
  describe('when some synth was issued', function () {
    const amount = parseEther('100')

    beforeEach('should issue', async function () {
      const depositAmount = parseEther('1000')
      await met.mint(user1.address, depositAmount)
      await met.connect(user1).approve(msdMET.address, ethers.constants.MaxUint256)
      await msdMET.connect(user1).deposit(depositAmount, user1.address)
      await msUSDDebt.connect(user1).issue(amount, user1.address)
    })
```
