### Title
Uninitialized `accountIndexOf` read as literal `0` inflates reward accrual when token index equals `INITIAL_INDEX`, allowing reward-token theft - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
CVE-2020-24977 is a global buffer over-read in libxml2's `xmlEncodeEntitiesInternal`: a missing bounds check causes the code to read past the end of a buffer and treat out-of-bounds bytes as valid data. The Metronome analog is in `RewardsDistributor._calculateTokenDelta`: when a user's `accountIndexOf` slot is uninitialized (the `0` sentinel), the code "reads past" the valid data and treats `0` as a literal account index unless the global token index is *strictly greater* than `INITIAL_INDEX`. When the stored token index is exactly `INITIAL_INDEX` (e.g., in the same block rewards are enabled, or whenever `totalSupply == 0` prevented index growth), the missing fallback produces a `_deltaIndex` of `1e18`, crediting the account `balanceOf(account)` reward tokens out of thin air.

### Finding Description
`accountIndexOf[token_][account_]` uses `0` as a sentinel meaning "account has never been checkpointed". The guard that is supposed to substitute `INITIAL_INDEX` for fresh accounts is: [1](#0-0) 

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The comparison uses strict `>` instead of `>=`. When `tokenStates[token_].index == INITIAL_INDEX`:

- `_accountIndex` stays `0` (the uninitialized sentinel read as valid data — the over-read analog).
- `_deltaIndex = INITIAL_INDEX - 0 = 1e18`.
- `_tokensDelta = balanceOf(account).wadMul(1e18) = balanceOf(account)` — the account accrues reward tokens equal to its full DepositToken/DebtToken balance.

The stored index remains exactly `INITIAL_INDEX` in two reachable states:

1. **Same-block window**: when `updateTokenSpeed`/`updateTokenSpeeds` first sets a token's state to `TokenState({index: INITIAL_INDEX, ...})`, `_calculateTokenIndex` returns `(0,0)` while `_deltaTimestamps == 0`, so the index stays `INITIAL_INDEX` for the whole block. Any pre-existing token holder (holders before the distributor was enabled, common since `RewardsDistributor` is added to already-live pools) can claim inflated rewards in that block. [2](#0-1) 

2. **Zero-supply window**: if `token_.totalSupply() == 0`, `_calculateTokenIndex` computes `_ratio = 0`, so `_newIndex` stays pinned at `INITIAL_INDEX` indefinitely while `timestamp` advances. [3](#0-2) 

Note the attack does **not** require the attacker to mint or transfer: `claimRewards(address[] accounts_, IERC20[] tokens_)` is permissionless and iterates attacker-chosen accounts/tokens. [4](#0-3) 

The payout path `_transferRewardIfEnoughTokens` then transfers `tokensAccruedOf[account]` reward tokens as long as the distributor holds enough balance. [5](#0-4) 

### Impact Explanation
An unprivileged attacker holding any balance of a reward-tracked `DepositToken` or `DebtToken` (acquired by depositing/borrowing through normal public pool functions, or by simply already holding when the distributor is enabled) can call `claimRewards` while `tokenStates[token].index == INITIAL_INDEX` and be credited `balanceOf(attacker)` reward tokens — up to draining the entire `rewardToken` balance of the distributor in a single call. This is direct theft of unclaimed yield earmarked for all users; honest users' accrued rewards are stolen and subsequent claims silently no-op due to the insufficient-balance check in `_transferRewardIfEnoughTokens`.

### Likelihood Explanation
The condition `index == INITIAL_INDEX` is not exotic: it holds for the entire block in which a reward token's speed is first set (front-runnable in a public mempool / bundle after the governor's `updateTokenSpeed` tx — the exploit itself uses only public entry points), and it holds persistently whenever a tracked token has `totalSupply == 0`, which is normal for a newly added deposit token on a fresh pool. Once the index advances past `INITIAL_INDEX` the window closes, but a single successful call drains the whole reward balance. Attack cost is one public call; no privileged role, oracle manipulation, or malicious endpoint is required.

### Recommendation
Change the fallback condition to cover the boundary case:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Equivalently, treat a `0` account index as `INITIAL_INDEX` unconditionally (`_accountIndex = _tokenIndex` or `INITIAL_INDEX`, whichever makes delta 0 for a fresh account). Additionally, in `_updateTokenSpeed`, consider initializing `tokenStates[token_]` only when speed is actually activated, and add a regression test asserting `claimable(account)` is 0 for a never-checkpointed holder in the same block as `updateTokenSpeed`.

### Proof of Concept
Foundry fork test sketch:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {RewardsDistributor} from "../contracts/RewardsDistributor.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract UninitializedIndexOverRead is Test {
    RewardsDistributor distributor; // deployed against live pool
    IERC20 rewardToken;
    IERC20 depositToken;            // e.g. msUSD-dtoken with speed enabled
    address attacker = address(0xA11CE);

    function testInflatedAccrual() public {
        // Precondition: attacker holds depositToken (deposit/borrowed normally),
        // and governor has NOT yet enabled speed for it.

        // Governor enables rewards for depositToken (recorded at INITIAL_INDEX).
        vm.prank(governor);
        distributor.updateTokenSpeed(depositToken, 1e18); // same block

        // Index is still exactly INITIAL_INDEX this block; accountIndexOf == 0.
        uint256 attackerBal = depositToken.balanceOf(attacker);
        assertEq(distributor.claimable(attacker), attackerBal); // BUG: inflated

        uint256 rewardBefore = rewardToken.balanceOf(attacker);
        vm.prank(attacker);
        distributor.claimRewards(attacker);

        // Attacker receives balanceOf(attacker) reward tokens, not earned yield.
        assertEq(rewardToken.balanceOf(attacker) - rewardBefore, attackerBal);
    }
}
```

The same test succeeds with an arbitrary `attacker` that held `depositToken` before the distributor was ever registered, since `accountIndexOf` is `0` for every pre-existing holder and the `> INITIAL_INDEX` fallback never fires while the index is pinned at `INITIAL_INDEX`.

### Citations

**File:** contracts/RewardsDistributor.sol (L150-168)
```text
    function claimRewards(address[] memory accounts_, IERC20[] memory tokens_) public override nonReentrant {
        uint256 _accountsLength = accounts_.length;
        uint256 _tokensLength = tokens_.length;
        for (uint256 i; i < _tokensLength; ++i) {
            IERC20 _token = tokens_[i];

            if (tokenStates[_token].index > 0) {
                _updateTokenIndex(_token);
                for (uint256 j; j < _accountsLength; j++) {
                    _updateTokensAccruedOf(_token, accounts_[j]);
                }
            }
        }

        for (uint256 j; j < _accountsLength; j++) {
            address _account = accounts_[j];
            _transferRewardIfEnoughTokens(_account, tokensAccruedOf[_account]);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L201-207)
```text
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
```

**File:** contracts/RewardsDistributor.sol (L222-230)
```text
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```

**File:** contracts/RewardsDistributor.sol (L296-299)
```text
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
