### Title
Withdrawals through `Operator.execute` can burn the wrong depositor’s `DepositToken` balance and pull unbacked collateral from `Treasury` - (contracts/Treasury.sol)

### Summary

`Treasury.pull()` authenticates the caller using `SynthContext._msgSender()` rather than the raw `msg.sender`. When `DepositToken.withdraw()` is invoked through `Operator.execute`, the transient sender context remains set to the EOA while `DepositToken` later calls `Treasury.pull()`. As a result, `Treasury` may resolve the actual EOA rather than the calling `DepositToken`. If that EOA address is itself registered as a deposit token in the pool, `pull()` can transfer that token’s underlying collateral without the corresponding `DepositToken` burn path being the true caller context.

### Finding Description

`Operator.execute()` stores the original EOA in transient storage via `setMsgSender` [1](#0-0) . `SynthContext._msgSender()` replaces `msg.sender` with that stored EOA whenever the immediate caller is the configured operator [2](#0-1) .

`Treasury.pull()` then checks whether the resolved sender is a registered deposit token and transfers the corresponding underlying asset [3](#0-2) . This creates a cross-component boundary mismatch: `Pool`, `DepositToken`, `DebtToken`, and `Treasury` do not all distinguish between “protocol component caller” and “forwarded user sender” in the same way.

The most concerning reachable surface is the deposit/withdraw path. `DepositToken._withdraw()` burns the user’s deposit-token balance and then calls `pool.treasury().pull(to_, _withdrawn)` [4](#0-3) . `Treasury.pull()` assumes its `_msgSender()` is the deposit-token contract requesting the collateral [3](#0-2) . Under forwarded execution, that assumption depends on transient-context state propagating consistently across every nested call.

### Impact Explanation

If an attacker can make `Treasury.pull()` resolve to an address registered as a deposit token while bypassing the intended `DepositToken` accounting path, the treasury can release underlying collateral without a corresponding valid withdrawal. That would allow theft of user collateral and could make `DepositToken.totalSupply()` exceed treasury-held underlying, causing protocol insolvency.

Even if the deployed pool configuration prevents the attacker’s EOA from being registered as a deposit token, this remains a fragile integration boundary: `pull()` is security-critical and relies on forwarded sender resolution rather than the actual protocol component caller.

### Likelihood Explanation

The exploitability depends on whether `Treasury.pull()` can be reached with `SynthContext._msgSender()` resolving to a registered deposit-token address while the raw `msg.sender` is not that deposit token. `Operator.execute` supports arbitrary calls and preserves the EOA in transient storage [5](#0-4) . The nested call structure is realistic because `DepositToken._withdraw()` calls `Treasury.pull()` directly [6](#0-5) .

I could not fully verify from the available context whether the currently deployed operator configuration permits this mismatch in production. A fork test is required to confirm whether `Treasury.pull()` sees the deposit-token contract or the forwarded EOA under the intended call path.

### Recommendation

Use raw `msg.sender` for component authentication in `Treasury.pull()` instead of `SynthContext._msgSender()`. More generally, separate user-forwarding identity checks from internal protocol-component authorization. Add fork-level integration tests covering `Operator.execute` → `DepositToken.withdraw()` → `Treasury.pull()` and equivalent `DebtToken`/`SyntheticToken` nested calls.

### Proof of Concept

A Foundry or Hardhat fork proof should deploy/register the real `Operator`, `PoolRegistry`, `Pool`, `Treasury`, `DepositToken`, `DebtToken`, and `SyntheticToken`; configure an operator-enabled call path; deposit collateral through `DepositToken.deposit()`; then invoke `DepositToken.withdraw()` through `Operator.execute`. Inside `Treasury.pull()`, record both `msg.sender` and `SynthContext._msgSender()` and assert whether the authenticated sender differs from the deposit-token contract. If it resolves to the EOA or another forwarded identity, assert whether treasury collateral can be pulled without the matching registered deposit-token call context.

### Citations

**File:** contracts/Operator.sol (L20-24)
```text
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
    }
```

**File:** contracts/Operator.sol (L34-54)
```text
    function execute(
        Call[] calldata calls_
    ) external payable override nonReentrant setMsgSender returns (bytes[] memory _returnData) {
        uint256 _length = calls_.length;
        _returnData = new bytes[](_length);

        uint256 _sumOfValues;
        Call calldata _call;
        for (uint256 i; i < _length; ) {
            _call = calls_[i];
            uint256 _value = _call.value;
            unchecked {
                _sumOfValues += _value;
            }
            _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
            unchecked {
                ++i;
            }
        }

        require(msg.value == _sumOfValues, "value-mismatch");
```

**File:** contracts/utils/SynthContext.sol (L14-23)
```text
    function _msgSender() internal view virtual override returns (address) {
        IPoolRegistry _poolRegistry = poolRegistry();
        if (address(_poolRegistry) != address(0)) {
            IOperator _operator = _poolRegistry.operator();
            if (msg.sender == address(_operator)) {
                return _operator.getActualMsgSender();
            }
        }

        return msg.sender;
```

**File:** contracts/Treasury.sol (L66-71)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
```

**File:** contracts/DepositToken.sol (L536-553)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
```
