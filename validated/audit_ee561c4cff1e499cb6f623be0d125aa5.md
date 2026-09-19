This confirms Starlark has no exception-catching mechanism at all: `try`, `except`, `finally`, and `raise` are explicitly unsupported, with the parser producing an explicit error `'try' not supported, all exceptions are fatal` for any attempted try statement in Starlark source. [1](#0-0) [2](#0-1) 

This structurally rules out the vm2 analog. The vm2 vulnerability depends on sandboxed JavaScript code being able to (1) `catch` a thrown `Error` object, (2) traverse into an unsanitized `.cause` property, and (3) reach a live reference to a powerful host object (like `process`) that exposes further callable methods (`require`, `execSync`) — because JS objects passed across the sandbox boundary remain live, dynamically-typed, and reflectively walkable.

Starlark's `EvalException` mechanism is fundamentally different:
- There is no `except`/`catch` construct in Starlark at all, so evaluation errors are always fatal to the running `.bzl`/`BUILD` evaluation — there is no code path where Starlark script logic can intercept an exception object and inspect its fields. [3](#0-2) 
- Even where Java-level exception chaining exists (`EvalException`'s constructor taking a `cause`, or `UncheckedEvalException`/`UncheckedEvalError` wrapping arbitrary host exceptions), these `cause` values are plain Java `Throwable` objects accessible only via `getCause()` on the Java side — they are never converted into Starlark values, never returned to Starlark scripts, and never exposed as an object with callable methods reachable from `.bzl` code. [4](#0-3) 
- `MethodDescriptor.call` unwraps `InvocationTargetException` and converts arbitrary checked host exceptions into a new `EvalException(e)`, but this only carries forward a string message (`getCauseMessage`), not a live reference to the host exception object that Starlark code could dereference.
<invoke name="codebase_search">
<parameter name="query">placeholder</parameter>
</invoke>

### Citations

**File:** src/test/java/net/starlark/java/syntax/ParserTest.java (L2260-2265)
```java
  @Test
  public void testTryStatementInBuild() throws Exception {
    setFailFast(false);
    parseFile("try: pass");
    assertContainsError("'try' not supported, all exceptions are fatal");
  }
```

**File:** docs/rules/language.mdx (L151-160)
```text
The following Python features are not supported:

* implicit string concatenation (use explicit `+` operator).
* Chained comparisons (such as `1 < x < 5`).
* `class` (see [`struct`](/rules/lib/builtins/struct#struct) function).
* `import` (see [`load`](/extending/concepts#loading-an-extension) statement).
* `while`, `yield`.
* generators and generator expressions.
* `is` (use `==` instead).
* `try`, `raise`, `except`, `finally` (see [`fail`](/rules/lib/globals#fail) for fatal errors).
```

**File:** src/main/java/net/starlark/java/eval/EvalException.java (L42-73)
```java
  /** Constructs an EvalException. Use {@link Starlark#errorf} if you want string formatting. */
  public EvalException(String message) {
    this(message, /* cause= */ (Throwable) null, /* includeStackTrace= */ true);
  }

  /**
   * Constructs an EvalException with a message and optional cause and bool indicating if the error
   * should contain a stack trace.
   *
   * <p>The cause does not affect the error message, so callers should incorporate {@code
   * cause.getMessage()} into {@code message} if desired, or call {@code EvalException(Throwable)}.
   */
  public EvalException(String message, @Nullable Throwable cause, boolean includeStackTrace) {
    super(checkNotNull(message), cause);
    this.includeStackTrace = includeStackTrace;
  }

  /**
   * Constructs an EvalException with a message and optional cause and defaulting stack trace to
   * true.
   *
   * <p>The cause does not affect the error message, so callers should incorporate {@code
   * cause.getMessage()} into {@code message} if desired, or call {@code EvalException(Throwable)}.
   */
  public EvalException(String message, @Nullable Throwable cause) {
    this(checkNotNull(message), cause, /* includeStackTrace= */ true);
  }

  /** Constructs an EvalException using the same message as the cause exception. */
  public EvalException(Throwable cause) {
    this(getCauseMessage(cause), cause, /* includeStackTrace= */ true);
  }
```

**File:** src/main/java/net/starlark/java/eval/Starlark.java (L1000-1026)
```java
  /**
   * Decorates a {@link RuntimeException} with its Starlark stack, to help maintainers locate
   * problematic source expressions.
   *
   * <p>The original exception can be retrieved using {@link #getCause}.
   */
  public static final class UncheckedEvalException extends RuntimeException {

    private UncheckedEvalException(RuntimeException cause, StarlarkThread thread) {
      super(createUncheckedEvalMessage(cause, thread), cause);
      thread.fillInStackTrace(this);
    }
  }

  /**
   * Decorates an {@link Error} with its Starlark stack, to help maintainers locate problematic
   * source expressions.
   *
   * <p>The original exception can be retrieved using {@link #getCause}.
   */
  public static final class UncheckedEvalError extends Error {

    private UncheckedEvalError(Error cause, StarlarkThread thread) {
      super(createUncheckedEvalMessage(cause, thread), cause);
      thread.fillInStackTrace(this);
    }
  }
```
