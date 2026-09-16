### Title
Exposure of Internal Java Exception Class Names in gRPC TriggerSmartContract/TriggerConstantContract Error Responses - (File: framework/src/main/java/org/tron/core/services/RpcApiService.java)

### Summary
The gRPC `WalletApi` implementation of `callContract` (used for `TriggerSmartContract` and `TriggerConstantContract`) catches generic `RuntimeException` and `Exception` and returns the fully-qualified internal exception class name concatenated with the exception message directly to the unauthenticated caller in the response `message` field, instead of a sanitized error string.

### Finding Description
In `callContract`, any unhandled `RuntimeException` or `Exception` thrown during contract execution is surfaced verbatim, including the Java class name, to the API caller: [1](#0-0) 

This is directly analogous to the OPC UA advisory's CWE-200/CWE-209 class of bug: a remote, unprivileged client (any address triggering a smart contract call via gRPC `TriggerSmartContract`/`TriggerConstantContract`, or the constant-call/estimate-gas HTTP and JSON-RPC paths that route through the same `Wallet`/VM layer) can induce specific internal exceptions and learn internal implementation details — exact Java exception class (including package path), which can reveal internal code structure, third-party library versions, or unexpected internal state (e.g., NPEs, `ArrayIndexOutOfBoundsException`, or other unanticipated runtime faults deep in the VM/actuator stack) that would otherwise only be visible in server logs. The same `e.getClass()`-into-response pattern also exists in `Wallet.getTransactionApprovedList`: [2](#0-1) 

Other adjacent code in the same files (`HttpService.java`'s `OversizedRequestErrorHandler`, `JsonRpcServlet.handleSingle`'s `RuntimeException` handling, and `TronJsonRpcImpl`'s JSON-RPC error paths) shows the project has already been deliberately hardened against exactly this class of leak (explicit comments about "must not leak exception class / stack frames" and returning generic "Internal error" messages): [3](#0-2) [4](#0-3) 

This indicates the `e.getClass()` leaks in `RpcApiService.callContract` and `Wallet.getTransactionApprovedList` are inconsistent with the project's own established mitigation pattern for the same bug class.

### Impact Explanation
Exposure of internal exception class names is server-side information disclosure. It does not by itself grant unauthorized fund movement, RCE, or a chain halt, so this stays at the "sensitive information exposure" tier that the OPC UA advisory itself is rated Medium for (CVSS C:L, no I/A impact). The practical impact here is reconnaissance value for an attacker crafting further exploits against the TVM/actuator internals (fingerprinting exception types/versions, or distinguishing different internal failure modes that should be indistinguishable to a remote caller).

### Likelihood Explanation
High likelihood of triggering: any account can call `TriggerConstantContract`/`TriggerSmartContract` over gRPC (no signature required for constant calls) and construct contract bytecode/inputs designed to throw unexpected `RuntimeException`s during VM execution, hitting the `catch (RuntimeException e)` / `catch (Exception e)` branches in `callContract`.

### Recommendation
In `RpcApiService.callContract`, stop concatenating `e.getClass()` into the response message returned to the caller; log the full exception (class + message + stack) server-side only, and return a generic, sanitized message (e.g., "Contract execute error" or the exception message alone, without the class name) to the client — consistent with the pattern already used in `HttpService`'s `OversizedRequestErrorHandler` and `JsonRpcServlet`'s internal-error handling. Apply the same fix to the equivalent `catch (Exception ex)` branch in `Wallet.getTransactionApprovedList`.

### Proof of Concept
1. As an unauthenticated/unprivileged client, send a gRPC `TriggerConstantContract` (or `TriggerSmartContract`) request against a contract address/input crafted to cause an unexpected runtime fault deep in VM execution (e.g., malformed constant-call arguments that trigger an internal `NullPointerException`/`ArrayIndexOutOfBoundsException` rather than an expected `ContractValidateException`).
2. Observe the gRPC response `Return.message` field contains `"<full.java.ClassName> : <exception message>"`, per: [1](#0-0) 
3. Compare against `TransactionApprovedList` flow via `getTransactionApprovedList`, which leaks the same information via `ex.getClass() + " : " + ex.getMessage()`: [2](#0-1)

### Citations

**File:** framework/src/main/java/org/tron/core/services/RpcApiService.java (L237-246)
```java
    } catch (RuntimeException e) {
      retBuilder.setResult(false).setCode(response_code.CONTRACT_EXE_ERROR)
          .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
      trxExtBuilder.setResult(retBuilder);
      logger.warn("When run constant call in VM, have RuntimeException: " + e.getMessage());
    } catch (Exception e) {
      retBuilder.setResult(false).setCode(response_code.OTHER_ERROR)
          .setMessage(ByteString.copyFromUtf8(e.getClass() + " : " + e.getMessage()));
      trxExtBuilder.setResult(retBuilder);
      logger.warn(UNKNOWN_EXCEPTION_CAUGHT + e.getMessage(), e);
```

**File:** framework/src/main/java/org/tron/core/Wallet.java (L694-697)
```java
      } catch (Exception ex) {
        resultBuilder.setCode(TransactionApprovedList.Result.response_code.OTHER_ERROR);
        resultBuilder.setMessage(ex.getClass() + " : " + ex.getMessage());
      }
```

**File:** framework/src/main/java/org/tron/common/application/HttpService.java (L103-117)
```java
  /**
   * For oversized requests (the 413 thrown by SizeLimitHandler during dispatch) logs the
   * detail server-side and returns the short, uniform bad-message page, instead of the
   * default error page that leaks the exception stack and internal request sizes. All
   * other errors keep Jetty's default handling.
   */
  private static final class OversizedRequestErrorHandler extends ErrorHandler {

    @Override
    public void handle(String target, Request baseRequest, HttpServletRequest request,
        HttpServletResponse response) throws IOException, ServletException {
      if (response.getStatus() == HttpStatus.PAYLOAD_TOO_LARGE_413) {
        Throwable cause = (Throwable) request.getAttribute(RequestDispatcher.ERROR_EXCEPTION);
        logger.info("Reject oversized request, uri: {}, detail: {}",
            request.getRequestURI(), cause == null ? "413" : cause.getMessage());
```

**File:** framework/src/main/java/org/tron/core/services/jsonrpc/JsonRpcServlet.java (L157-164)
```java
    try {
      rpcServer.handle(cachedReq, bufferedResp);
    } catch (RuntimeException e) {
      logger.error("RPC execution failed", e);
      writeJsonRpcError(resp, JsonRpcError.INTERNAL_ERROR, "Internal error",
          rootNode.get("id"), false);
      return;
    }
```
