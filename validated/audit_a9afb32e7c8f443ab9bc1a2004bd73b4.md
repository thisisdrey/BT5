### Title
`DebtToken._burn` snapshots `balanceOf` at the fresh interest index but stores the stale `debtIndex`, double-charging accrued interest when `Pool.liquidate` burns debt without a prior `accrueInterest()` - (File: contracts/DebtToken.sol)

### Summary
The analog to the FairSide finding is in `DebtToken`. Every state-mutating entry point that changes a borrower's principal — `issue`, `flashIssue`, `mint`, `repay`, `repayAll` — calls `accrueInterest()` first so that `debtIndex` is current when `debtIndexOf[account_]` is re-snapshotted. The `burn(address, uint256)` path (restricted to `onlyPool`, invoked by liquidation) does not accrue interest. `_burn` computes the remaining principal using `balanceOf()`, which internally uses the *freshly computed* index, but then persists `debtIndexOf[account_] = debtIndex` — the *stale* stored index. The interest accrued since `lastTimestampAccrued` is therefore already baked into `principalOf`, yet it will be counted a second time by the `principalOf * currentIndex / debtIndexOf` ratio in every subsequent `balanceOf()`.

### Finding Description
In `accrueInterest()`, `debtIndex` and `lastTimestampAccrued` are only updated when the function is invoked [1](#0-0) . `balanceOf` computes debt lazily via `_calculateInterestAccrual()`, so reads are correct regardless [2](#0-1) . However `_burn` mixes the two clocks:

```solidity
uint256 _accountBalance = balanceOf(account_);   // uses fresh, in-memory index
principalOf[account_] = _accountBalance - amount_;
debtIndexOf[account_] = debtIndex;                // stale stored index
``` [3](#0-2) 

Compare with `repay`/`repayAll`, which call `accrueInterest()` before `_burn`, keeping the stored `debtIndex` in sync with the principal snapshot [4](#0-3) [5](#0-4) . The `burn` entry point used by `Pool` during liquidation has no such call [6](#0-5) .

### Impact Explanation
After a partial liquidation where no `accrueInterest()` ran earlier in the same block, the victim's remaining `principalOf` already includes all interest accrued up to `block.timestamp`, but `debtIndexOf[account_]` still points to the old index. The delta `currentIndex / staleIndex` is applied again to that principal, inflating the remaining debt by roughly `principal * interestRate * elapsed`. This pushes the position further underwater (lower health, larger `debtOf`), enables further liquidations on debt the user does not owe, and if the user later repays they must burn more synthetic tokens than their true debt — direct loss of user funds and a broken debt-conservation invariant (borrower debt no longer equals `principal` grown by the index from the correct checkpoint).

### Likelihood Explanation
Liquidation via `Pool.liquidate` → `DebtToken.burn` is a public, unprivileged path — any EOA can liquidate an unhealthy account. The bug triggers whenever a partial liquidation lands in a block where `accrueInterest()` was not already called by another `issue`/`repay`/`accrueInterest` transaction and `interestRate > 0`. Interest-bearing debt tokens (`interestRate` set by governance, e.g. 2–50% in tests) are a standard deployed configuration [7](#0-6) .

Caveat: `Pool.liquidate` could itself call `debtToken.accrueInterest()` before `burn()` — the index didn't let me confirm the exact call sequence inside `Pool.sol` (the file matched 108 relevant identifiers but its body wasn't retrieved). If `Pool.liquidate` does accrue first, this specific instance collapses; the missing `accrueInterest()` in `burn` itself is still the divergence versus every other mutating entry point.

### Recommendation
Move `accrueInterest()` into `_burn`/`_mint` (or call it at the top of `burn`), mirroring the FairSide fix of moving `_updateConvictionScore()` outside the conditional, so the interest index is refreshed unconditionally before `debtIndexOf[account_]` is re-snapshotted. At minimum, add `accrueInterest()` to `burn` and to `Pool.liquidate` before debt accounting.

### Proof of Concept
Hardhat sketch against the repo's own fixtures (adapted from `test/DebtToken.test.ts` and `test/E2E.base.next.test.ts`):

```ts
// alice deposits collateral and issues msUSD debt
await msdUSDC.connect(alice).deposit(parseUnits('400', 6), alice.address)
await msUSDDebt.connect(governor).updateInterestRate(parseEther('0.5')) // 50% APR
await msUSDDebt.connect(alice).issue(parseEther('100'), alice.address)

// time passes; interest accrues but accrueInterest() is NOT called
await time.increase(time.duration.minutes(10))
const debtBefore = await msUSDDebt.balanceOf(alice.address)

// bob partially liquidates alice -> Pool.liquidate -> debtToken.burn (no accrueInterest inside)
await msdUSDC.connect(bob).deposit(parseUnits('400', 6), bob.address)
await msUSDDebt.connect(bob).issue(parseEther('100'), bob.address)
const repayAmt = debtBefore.div(2)
await pool.connect(bob).liquidate(msUSD.address, alice.address, repayAmt, msdUSDC.address)

// Immediately after: remaining principal already embeds accrued interest,
// but debtIndexOf[alice] is stale -> balanceOf double-counts the elapsed interest.
const principalAfter = await msUSDDebt.principalOf(alice.address)       // ~debtBefore/2, interest included
const balanceAfter  = await msUSDDebt.balanceOf(alice.address)
assert(balanceAfter.gt(principalAfter))                                  // nonzero phantom interest already
// and it keeps growing: balanceAfter > debtBefore - repayAmt - (liquidation fee effects)
```

Expected observable: `balanceOf(alice)` right after liquidation is strictly greater than the true remaining debt (`debtBefore - repaid`), and `pool.debtOf(alice)` exceeds it, demonstrating the double-counted interest window.

### Citations

**File:** contracts/DebtToken.sol (L156-179)
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
```

**File:** contracts/DebtToken.sol (L196-205)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
```

**File:** contracts/DebtToken.sol (L213-215)
```text
    function burn(address from_, uint256 amount_) external override onlyPool {
        _burn(from_, amount_);
    }
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

**File:** contracts/DebtToken.sol (L476-492)
```text
        accrueInterest();

        _repaid = balanceOf(onBehalfOf_);
        if (_repaid == 0) revert AmountIsZero();

        address _msgSender = _msgSender();
        ISyntheticToken _syntheticToken = syntheticToken;

        uint256 _amount;
        (_amount, _fee) = quoteRepayIn(_repaid);

        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L528-535)
```text
        uint256 _accountBalance = balanceOf(account_);
        if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

        unchecked {
            principalOf[account_] = _accountBalance - amount_;
            debtIndexOf[account_] = debtIndex;
            totalSupply_ -= amount_;
        }
```

**File:** test/E2E.base.next.test.ts (L363-398)
```typescript
    it('should increase debt by the time', async function () {
      // given
      await msdUSDC.deposit(parseUnits('500', await usdc.decimals()), alice.address)
      await msUSDDebt.issue(parseEther('100'), alice.address)
      const debtBefore = await msUSDDebt.balanceOf(alice.address)

      // when
      const interestRate = parseEther('0.02') // 2%
      if (!(await msUSDDebt.interestRate()).eq(interestRate)) {
        await msUSDDebt.connect(governor).updateInterestRate(interestRate)
      }

      await time.increase(time.duration.years(1))
      await msUSDDebt.accrueInterest()

      // then
      const expectedDebt = debtBefore.mul(parseEther('1').add(interestRate)).div(parseEther('1'))
      expect(await pool.debtOf(alice.address)).closeTo(expectedDebt, parseEther('0.01'))
    })

    it('should liquidate unhealthy position', async function () {
      // given
      await msdUSDC.deposit(parseUnits('400', await usdc.decimals()), alice.address)
      await msUSDDebt.connect(governor).updateInterestRate(parseEther('0')) // 0%
      const {_issuableInUsd} = await pool.debtPositionOf(alice.address)
      await msUSDDebt.issue(_issuableInUsd, alice.address)
      await msUSDDebt.connect(governor).updateInterestRate(parseEther('0.5')) // 50%
      await time.increase(time.duration.minutes(10))
      await msUSDDebt.accrueInterest()
      expect((await pool.debtPositionOf(alice.address))._isHealthy).false

      // when
      await msdUSDC.deposit(parseUnits('400', await usdc.decimals()), bob.address)
      await msUSDDebt.connect(bob).issue(parseEther('100'), bob.address)
      const amountToRepay = parseEther('50') // repay all user's debt
      const tx = await pool.connect(bob).liquidate(msUSD.address, alice.address, amountToRepay, msdUSDC.address)
```
