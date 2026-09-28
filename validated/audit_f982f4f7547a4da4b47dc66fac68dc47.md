### Title
Per-account debt dust is truncated while aggregate `totalSupply_` dust is not, permanently blocking debt-token removal — ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken._burn` truncates each user's balance dust toward zero (a repaid-in-full account ends with `principalOf == 0` and `balanceOf == 0`), but `totalSupply_` is only decremented by the *rounded-down* `balanceOf` amount. The residual rounding dust has no owner, can never be burned, and keeps compounding through `accrueInterest`. This mirrors the audited inconsistency: dust is truncated on the per-account side but not on the aggregate debt side, breaking `Pool.removeDebtToken`'s `TotalSupplyIsNotZero` invariant.

### Finding Description
`DebtToken.balanceOf` computes the live balance as `(principalOf[account] * _debtIndex) / debtIndexOf[account]`, which truncates toward zero [1](#0-0) . When a user fully repays via `repayAll` (or repays the exact `balanceOf` via `repay`, or is fully liquidated via `Pool.liquidate`), `_burn` executes:

```solidity
// contracts/DebtToken.sol:528-541
uint256 _accountBalance = balanceOf(account_);        // rounded down
principalOf[account_] = _accountBalance - amount_;    // 0
debtIndexOf[account_] = debtIndex;
totalSupply_ -= amount_;                              // subtracts rounded value only
``` [2](#0-1) 

Meanwhile `accrueInterest` grows `totalSupply_` by the full accrued amount and grows `debtIndex` multiplicatively [3](#0-2) . The per-account truncation in `balanceOf` means `sum(balanceOf(account)) < totalSupply_` by up to ~1 wei per account per full repayment, and the difference is permanently stranded in `totalSupply_`: no account's `principalOf` corresponds to it, so there is no reachable code path (`repay`, `repayAll`, `liquidate`, `DebtToken.burn`) that can decrement it — any further `_burn` would need a real account balance.

`Pool.removeDebtToken` requires `debtToken_.totalSupply() == 0` [4](#0-3) , and `totalSupply()` adds the still-accruing unclaimed interest on top of the stranded `totalSupply_` dust [5](#0-4) . Once a single account has fully repaid after any interest accrual, `totalSupply()` is strictly positive forever and `removeDebtToken` permanently reverts with `TotalSupplyIsNotZero`.

### Impact Explanation
Permanent liveness/accounting failure reachable by any unprivileged user: once the dust is stranded, the debt token (and its synthetic token via `debtTokenOf`) can never be removed from the pool, and the phantom debt supply keeps accruing interest that is minted as free synthetic tokens to the `feeCollector` (`accrueInterest` mints `_interestAmountAccrued` on the inflated `totalSupply_` base) [6](#0-5) . The dust also counts against `maxTotalSupply` in `_mint` [7](#0-6) . This is a permanent break of the "supply returns to zero when all debt is cleared" invariant — the aggregate dust that was deliberately truncated per-account is never truncated at the total level.

### Likelihood Explanation
Certain to occur organically: any full repayment or full liquidation after interest accrual leaves ≥1 wei of unattributed supply whenever `principal * debtIndex / debtIndexOf` truncates. An attacker can also deliberately amplify it by creating many accounts, issuing minimum-viable debt, letting interest accrue, and calling `repayAll` from each — each iteration strands fresh dust. No privileged action is required; `repay`, `repayAll`, `issue`, and `liquidate` are all public entry points, and `whenNotShutdown`, `nonReentrant`, and the debt-floor checks do not prevent a full repayment that zeros an account.

### Recommendation
Truncate the aggregate dust symmetrically to the per-account truncation: in `_burn`, when `amount_ > 0 && balanceOf(account_) == 0` after the update, also snap any residual unattributed dust out of `totalSupply_` (or track supply as `sum(principal)` consistent with index math). Alternatively, relax `Pool.removeDebtToken` to tolerate dust (e.g., `totalSupply() < dustThreshold`) — though the cleaner fix is to make `_burn` decrement `totalSupply_` by the same un-rounded amount that the account's debt was conceptually charged, or zero `totalSupply_` when the last account's debt is cleared.

### Proof of Concept
Foundry test sketch (adapt to the repo's Hardhat fixture in `test/DebtToken.test.ts`, which already observes dust with `closeTo(..., dust)` assertions [8](#0-7) ):

```solidity
// assume: pool with deposit token (msdToken), synthetic msUSD + DebtToken msUSDDebt
// 1. alice deposits collateral, issues debt
msdToken.deposit(1000e18, alice);
msUSDDebt.issue(100e18, alice);            // alice borrows

// 2. let interest accrue so index math truncates
vm.warp(block.timestamp + 30 days);
msUSDDebt.accrueInterest();

// 3. alice fully repays via repayAll
msUSDDebt.repayAll(alice);

// 4. per-account dust truncated to zero...
assertEq(msUSDDebt.balanceOf(alice), 0);
assertEq(pool.getDebtTokensOfAccount(alice).length, 0);

// 5. ...but aggregate dust is stranded in totalSupply_ forever
uint256 dust = msUSDDebt.totalSupply();
assertGt(dust, 0);

// 6. governor can never remove the debt token
vm.prank(governor);
vm.expectRevert(TotalSupplyIsNotZero.selector);
pool.removeDebtToken(msUSDDebt);

// 7. and the dust keeps accruing feeCollector interest on phantom debt
vm.warp(block.timestamp + 365 days);
assertGt(msUSDDebt.totalSupply(), dust);   // stranded dust compounds
```

Caveat: whether step 5's dust is nonzero depends on interest-rate/timing producing a truncation remainder; with any nonzero `interestRate` and the integer division in `balanceOf`, a remainder is effectively guaranteed over enough accounts. I was not able to verify whether deployed `debtFloorInUsd` is nonzero (governance-set), but the dust mechanism is independent of the floor — the floor only gates partial repayments, not `repayAll`.

### Citations

**File:** contracts/DebtToken.sol (L156-180)
```text
    function accrueInterest() public override {
        (
            uint256 _interestAmountAccrued,
            uint256 _debtIndex,
            uint256 _lastTimestampAccrued
        ) = _calculateInterestAccrual();

        if (block.timestamp == _lastTimestampAccrued) {
            return;
        }

        lastTimestampAccrued = block.timestamp;

        if (_interestAmountAccrued > 0) {
            totalSupply_ += _interestAmountAccrued;
            debtIndex = _debtIndex;

            // Note: Address states where minting will fail (e.g. the token is inactive, it reached max supply, etc)
            try syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee) {
                pendingInterestFee = 0;
            } catch {
                pendingInterestFee += _interestAmountAccrued;
            }
        }
    }
```

**File:** contracts/DebtToken.sol (L196-206)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
    }
```

**File:** contracts/DebtToken.sol (L500-503)
```text
    function totalSupply() external view override returns (uint256) {
        (uint256 _interestAmountAccrued, , ) = _calculateInterestAccrual();
        return totalSupply_ + _interestAmountAccrued;
    }
```

**File:** contracts/DebtToken.sol (L525-543)
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

        emit Transfer(account_, address(0), amount_);

        // Remove this token from the debt tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf(account_) == 0) {
            pool.removeFromDebtTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DebtToken.sol (L590-591)
```text
        totalSupply_ += amount_;
        if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();
```

**File:** contracts/Pool.sol (L725-732)
```text
    function removeDebtToken(IDebtToken debtToken_) external onlyGovernor {
        if (debtToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();
        if (!debtTokens.remove(address(debtToken_))) revert DebtTokenDoesNotExist();

        delete debtTokenOf[debtToken_.syntheticToken()];

        emit DebtTokenRemoved(debtToken_);
    }
```

**File:** test/DebtToken.test.ts (L983-985)
```typescript
        await msUSDDebt.connect(user1).repayAll(user1.address)
        expect(await msUSDDebt.balanceOf(user1.address)).eq(0)
        expect(await msUSDDebt.totalSupply()).closeTo(await msUSDDebt.balanceOf(user2.address), dust)
```
