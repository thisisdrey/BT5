### Title
Accidental or excess tokens sent to `Treasury` are permanently locked — no rescue path exists for surplus underlying — ([File: contracts/Treasury.sol](metronome-synth-public--001/contracts/Treasury.sol))

### Summary
The referenced NounsDAO bug class is an asymmetric rescue design: `rescueERC20` protects against stray foreign tokens, but excess of the protocol's core token cannot be recovered without tearing down the whole position. Metronome exhibits a stronger version of the same asymmetry. `DepositToken` and `DebtToken` inherit `TokenHolder.sweep()` so the governor can recover stray tokens from them, but all actual collateral sits in `Treasury`, which inherits neither `TokenHolder` nor any equivalent rescue function. Tokens sent directly to `Treasury` — including a deposit token's `underlying` — have no extraction path: `pull()` is restricted to registered `DepositToken` callers, `claimFromVesper()` only handles reward tokens, and `migrateTo()` only runs during a treasury migration. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`Treasury` is the sole custodian of all deposited collateral: `DepositToken.deposit()` transfers `underlying` from the user into `address(pool.treasury())`, and `_withdraw()` releases it via `treasury.pull()`. [4](#0-3) [5](#0-4) 

`pull()` enforces that the caller is a registered `DepositToken`, so no one — not even the governor — can pull arbitrary tokens out. [6](#0-5)  `claimFromVesper()` only iterates a Vesper pool's configured reward tokens and only computes the transferable amount as `balanceOf(treasury) - depositToken.totalSupply()` when the reward token happens to be a collateral underlying. [7](#0-6)  Any other ERC20 or native asset sent to `Treasury` is permanently stuck unless a full `migrateTo()` is executed (the cancel-stream analog: recovering funds requires dismantling the protocol component). [8](#0-7) 

The surplus-underlying case is especially direct: if a user mistakenly `transfer()`s underlying (e.g., WETH, USDC) straight to `Treasury` instead of calling `DepositToken.deposit()`, those tokens inflate `balanceOf(treasury)` above `depositToken.totalSupply()` with no minted claim against them. Unlike `claimFromVesper()` — which is governor-only and reward-token-scoped — there is no general "skim the surplus" path, so the donation is locked in place until an eventual treasury migration. [9](#0-8) 

### Impact Explanation
Permanent freezing of funds: any ERC20 sent to `Treasury` outside the deposit flow (direct transfer, accidental `transfer` instead of `deposit`, fee-on-transfer remainder, airdrops) is unrecoverable. For surplus underlying the funds are additionally silently absorbed as unaccounted backing, masking the loss. The only recovery mechanism, `migrateTo()`, is the analogue of Nouns' "cancel the stream": it requires deploying and activating an entirely new treasury and transfers *all* underlying balances, not just the excess — an operationally heavy, disruptive action simply to recover a mistaken transfer. [10](#0-9) 

### Likelihood Explanation
Direct transfers to a well-known treasury address are a common user error (copy-paste of the wrong address, using `transfer` instead of the deposit flow, reward/airdrop distributions). The codebase itself acknowledges the surplus-balance scenario in `claimFromVesper()` (`_amount -= _depositToken.totalSupply()`), confirming excess collateral accumulates in `Treasury` in practice, yet provides no generic recovery for it. [9](#0-8)  Note this is a recoverability/liveness-of-funds issue rather than an unprivileged-attacker exploit — it requires no malicious actor, matching the source bug class.

### Recommendation
Give `Treasury` a governed rescue that preserves solvency accounting, mirroring the audit recommendation's "only the excess" check:

```solidity
function sweep(IERC20 token_, address to_, uint256 amount_) external onlyGovernor {
    if (to_ == address(0)) revert RecipientIsNull();
    IDepositToken _dt = pool.depositTokenOf(token_);
    if (address(_dt) != address(0)) {
        // Only allow sweeping the surplus over totalSupply backing
        require(amount_ <= token_.balanceOf(address(this)) - _dt.totalSupply(), "amount exceeds surplus");
    }
    token_.safeTransfer(to_, amount_);
}
```

This allows rescue of foreign tokens and of excess underlying while never touching user-backed collateral.

### Proof of Concept
Hardhat-style fork sketch:

```ts
// setup: pool, depositToken (underlying = USDC), treasury deployed
const treasury = await ethers.getContractAt("Treasury", await pool.treasury());
const user = await ethers.getImpersonatedSigner(USDC_WHALE);

// 1. User deposits normally — backing tracked
await usdc.connect(user).approve(depositToken.address, DEPOSIT);
await depositToken.connect(user).deposit(DEPOSIT, user.address);

// 2. User mistakenly sends USDC straight to Treasury
await usdc.connect(user).transfer(treasury.address, EXTRA);

// Treasury now holds DEPOSIT + EXTRA; totalSupply only covers DEPOSIT + fees
assert((await usdc.balanceOf(treasury.address)) === DEPOSIT + EXTRA);

// 3. No recovery path:
//    - treasury.pull(user, EXTRA) reverts SenderIsNotDepositToken (caller is an EOA)
//    - Treasury exposes no sweep/rescue function
//    - claimFromVesper only iterates Vesper reward tokens, not arbitrary ERC20
//    EXTRA is locked until a full migrateTo() to a new treasury is executed.

// 4. Same for a foreign token:
await dai.connect(user).transfer(treasury.address, DAI_AMOUNT);
// DAI_AMOUNT is permanently stuck — no function can move it out.
```

Key call sites: `DepositToken.deposit()` routes collateral to `Treasury` [11](#0-10) ; `Treasury.pull()` reverts for any non-DepositToken caller [6](#0-5) ; and `Treasury` never inherits `TokenHolder`/`sweep` [12](#0-11) , unlike `DepositToken` which does [13](#0-12) .

### Citations

**File:** contracts/utils/TokenHolder.sol (L37-45)
```text
    function sweep(IERC20 token_, address to_, uint256 amount_) external {
        _requireCanSweep();

        if (address(token_) == address(0)) {
            Address.sendValue(payable(to_), amount_);
        } else {
            token_.safeTransfer(to_, amount_);
        }
    }
```

**File:** contracts/Treasury.sol (L25-25)
```text
contract Treasury is Initializable, ReentrancyGuardDeprecated, ReentrancyGuardTransient, Manageable, TreasuryStorageV1 {
```

**File:** contracts/Treasury.sol (L44-72)
```text
    function migrateTo(address newTreasury_) external override onlyPool {
        if (newTreasury_ == address(0)) revert AddressIsNull();

        address[] memory _depositTokens = pool.getDepositTokens();
        uint256 _len = _depositTokens.length;

        for (uint256 i; i < _len; ++i) {
            IERC20 _underlying = IDepositToken(_depositTokens[i]).underlying();

            uint256 _underlyingBalance = _underlying.balanceOf(address(this));

            if (_underlyingBalance > 0) {
                _underlying.safeTransfer(newTreasury_, _underlyingBalance);
            }
        }
    }

    /**
     * @notice Pull token from the Treasury
     * @param to_ The transfer recipient
     * @param amount_ The transfer amount
     */
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
    }
```

**File:** contracts/Treasury.sol (L79-100)
```text
    function claimFromVesper(IVPool vPool_, address to_) external onlyGovernor {
        IPoolRewards _rewards = IPoolRewards(vPool_.poolRewards());
        _rewards.updateReward(address(this));
        _rewards.claimReward(address(this));

        IPool _pool = pool;
        address[] memory _rewardTokens = _rewards.getRewardTokens();
        uint256 _len = _rewardTokens.length;
        for (uint256 i; i < _len; ++i) {
            IERC20 _token = IERC20(_rewardTokens[i]);
            uint256 _amount = _token.balanceOf(address(this));

            // Note: If the reward token is a collateral, transfer the surpass balance only
            IDepositToken _depositToken = _pool.depositTokenOf(_token);
            if (address(_depositToken) != address(0)) {
                _amount -= _depositToken.totalSupply();
            }

            if (_amount > 0) {
                _token.safeTransfer(to_, _amount);
            }
        }
```

**File:** contracts/DepositToken.sol (L48-55)
```text
contract DepositToken is
    Initializable,
    ReentrancyGuardDeprecated,
    ReentrancyGuardTransient,
    TokenHolder,
    Manageable,
    DepositTokenStorageV1
{
```

**File:** contracts/DepositToken.sol (L219-227)
```text
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;
```

**File:** contracts/DepositToken.sol (L550-552)
```text
        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

```
