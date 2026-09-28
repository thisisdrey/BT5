### Title
`claimRewards()` and `RecurringAirdrop.claim()` cannot specify a recipient — a user blacklisted by the reward token permanently loses accrued rewards - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol), [File: contracts/utils/RecurringAirdrop.sol](contracts/utils/RecurringAirdrop.sol))

### Summary
Metronome's reward-claiming entry points always transfer the reward token to the accrued account itself (`account_` in `RewardsDistributor`, `msg.sender` in `RecurringAirdrop`). If that account is blacklisted by a blacklistable reward token (e.g., USDC), the transfer reverts and the accrued rewards can never be collected — there is no `recipient` parameter to redirect the payout. This is the same bug class as the Particle `collectLiquidity()` finding.

### Finding Description
In `RewardsDistributor`, `claimRewards` resolves rewards for arbitrary `accounts_` and pays out via `_transferRewardIfEnoughTokens`, which does `rewardToken.safeTransfer(account_, amount_)`. There is no way to specify an alternative recipient. [1](#0-0) [2](#0-1) 

Notably, `claimRewards` is permissionless — anyone can call it for any account — but the destination is always `account_` itself, so neither the owner nor anyone else can route the payout elsewhere.

The same pattern exists in `RecurringAirdrop.claim`: the leaf binds `msg.sender` and the payout goes to `msg.sender` via `_transferReward(msg.sender, _claimable)`. A blacklisted claimant cannot collect. [3](#0-2) 

By contrast, the collateral withdrawal path was designed correctly — `DepositToken.withdraw(uint256 amount_, address to_)` accepts an arbitrary recipient, showing the protocol otherwise avoids this class. [4](#0-3) 

### Impact Explanation
If the configured `rewardToken` (or airdrop token) is a blacklistable ERC-20 such as USDC and a user address is added to that token's blacklist, `safeTransfer` to the account reverts. Since `nonReentrant` `claimRewards` always reverts atomically, `tokensAccruedOf[account_]` remains non-zero but is unreachable: the user's unclaimed yield is permanently frozen in the `RewardsDistributor` contract. The same applies to unclaimed airdrop allocations in `RecurringAirdrop`.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires (a) a blacklistable token configured as the reward/airdrop token, and (b) the user's address being blacklisted by that token's issuer — an external condition not controllable by an attacker. However, the trigger is entirely out of the protocol's control and the freeze is permanent once it occurs. Notably, this exact bug class was judged "acknowledged" rather than fixed in the reference audit precisely because adding a recipient lets blacklisted users circumvent the blacklist — the same trade-off applies here, and arguably the current design is the intended one.

### Recommendation
If the protocol accepts the blacklist-circumvention trade-off, add a recipient parameter, e.g.:

```diff
- function claimRewards(address account_) external override {
-     claimRewards(account_, tokens);
+ function claimRewards(address account_, address recipient_) external override {
+     claimRewards(account_, tokens, recipient_);
```

with `_transferRewardIfEnoughTokens(recipient_, tokensAccruedOf[account_])` gated by `account_ == _msgSender()` or an approval. For `RecurringAirdrop`, encode `[account, recipient, amount]` or keep `msg.sender` binding and add a `to_` parameter. Alternatively, document the acknowledged risk that blacklisted accounts cannot claim, matching the reference finding's disposition.

### Proof of Concept
```solidity
// Fork test sketch (Foundry, mainnet fork where rewardToken = USDC)
function test_blacklistedUserCannotClaim() public {
    // 1. User accrues rewards (deposits collateral / holds msAsset or debt token)
    vm.prank(alice);
    depositToken.deposit(amount, alice);
    vm.warp(block.timestamp + 30 days);

    // 2. Alice is blacklisted on USDC (reward token)
    vm.prank(usdcBlacklister);
    usdc.blacklist(alice);

    // 3. Any attempt to claim reverts; there is no recipient alternative
    vm.expectRevert();
    rewardsDistributor.claimRewards(alice);

    // tokensAccruedOf[alice] stays > 0 forever
    assertGt(rewardsDistributor.tokensAccruedOf(alice), 0);
}
```

The same holds for `RecurringAirdrop.claim(amount_, proof_)` — a valid leaf/proof cannot be claimed once `msg.sender` is blacklisted by the airdrop token.

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

**File:** contracts/RewardsDistributor.sol (L248-256)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
    }
```

**File:** contracts/utils/RecurringAirdrop.sol (L52-66)
```text
    function claim(uint256 amount_, bytes32[] calldata proof_) external nonReentrant {
        if (merkleRoot == bytes32(0)) revert NothingToClaim();

        bytes32 _leaf = keccak256(abi.encodePacked(msg.sender, amount_));
        if (!MerkleProof.verify(proof_, merkleRoot, _leaf)) revert InvalidProof();

        uint256 _claimable = amount_ - claimed[msg.sender];
        if (_claimable == 0) revert NothingToClaim();

        claimed[msg.sender] += _claimable;

        _transferReward(msg.sender, _claimable);

        emit RewardClaimed(msg.sender, _claimable);
    }
```

**File:** contracts/DepositToken.sol (L406-412)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```
