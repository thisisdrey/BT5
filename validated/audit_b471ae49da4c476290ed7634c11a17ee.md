No vulnerability found for this question.

**Rationale:** The Grav SSTI report hinges on a privilege-separation model where template *authors* (editors) are only semi-trusted and a sandbox is supposed to prevent them from reaching dangerous PHP functions from within Twig — the flaw was that the sandbox's `isDangerousFunction()` check could be bypassed by mutating the Twig environment object itself via allowed template syntax.

Go's `html/template` and `text/template` packages have no such trust boundary to bypass. The package documentation explicitly states the opposite threat model: "The security model used by this package assumes that template authors are trusted, while Execute's data parameter is not." [1](#0-0) [2](#0-1) 

Given that, `text/template`'s `FuncMap`, `call` builtin, and method/field dispatch (`evalCall`, `safeCall`) are designed to let template authors call arbitrary registered functions and public methods on the data value by design — this is documented, intended behavior, not a sandbox escape. [3](#0-2) [4](#0-3) 

The only "sandbox" concept present is `html/template`'s contextual autoescaper, which protects against untrusted *data* values breaking out of HTML/JS/CSS/URL contexts (e.g. `isSafeURL`, `_html_template_*` escaper pipeline stages) — a different primitive (output-context escaping of untrusted data) than Grav's issue (privileged-function whitelist bypass via mutable sandbox object reachable from template authors). [5](#0-4) [6](#0-5) 

There is no equivalent mechanism in the Go template packages where a lower-privileged actor (analogous to the Grav "editor") is expected to be restricted from calling certain functions/methods while a higher-privileged actor controls an allowlist that can itself be tampered with through the templating language. Since the core premise (a bypassable function/method allowlist enforced against a semi-trusted template author) does not exist in this codebase, no valid analog vulnerability can be established without stretching the analogy beyond the actual code.

### Citations

**File:** src/html/template/doc.go (L34-36)
```go
The security model used by this package assumes that template authors are
trusted, while Execute's data parameter is not. More details are
provided below.
```

**File:** src/html/template/doc.go (L218-220)
```go
This package assumes that template authors are trusted, that Execute's data
parameter is not, and seeks to preserve the properties below in the face
of untrusted data:
```

**File:** src/text/template/exec.go (L777-800)
```go
// evalCall executes a function or method call. If it's a method, fun already has the receiver bound, so
// it looks just like a function call. The arg list, if non-nil, includes (in the manner of the shell), arg[0]
// as the function itself.
func (s *state) evalCall(dot, fun reflect.Value, isBuiltin bool, node parse.Node, name string, args []parse.Node, final reflect.Value) reflect.Value {
	if args != nil {
		args = args[1:] // Zeroth arg is function name/node; not passed to function.
	}
	typ := fun.Type()
	numIn := len(args)
	if !isMissing(final) {
		numIn++
	}
	numFixed := len(args)
	if typ.IsVariadic() {
		numFixed = typ.NumIn() - 1 // last arg is the variadic one.
		if numIn < numFixed {
			s.errorf("wrong number of args for %s: want at least %d got %d", name, typ.NumIn()-1, len(args))
		}
	} else if numIn != typ.NumIn() {
		s.errorf("wrong number of args for %s: want %d got %d", name, typ.NumIn(), numIn)
	}
	if err := goodFunc(name, typ); err != nil {
		s.errorf("%v", err)
	}
```

**File:** src/text/template/funcs.go (L310-324)
```go
// call returns the result of evaluating the first argument as a function.
// The function must return 1 result, or 2 results, the second of which is an error.
func call(name string, fn reflect.Value, args ...reflect.Value) (reflect.Value, error) {
	fn = indirectInterface(fn)
	if !fn.IsValid() {
		return reflect.Value{}, fmt.Errorf("call of nil")
	}
	typ := fn.Type()
	if typ.Kind() != reflect.Func {
		return reflect.Value{}, fmt.Errorf("non-function %s of type %s", name, typ)
	}

	if err := goodFunc(name, typ); err != nil {
		return reflect.Value{}, err
	}
```

**File:** src/html/template/url.go (L45-54)
```go
// isSafeURL is true if s is a relative URL or if URL has a protocol in
// (http, https, mailto).
func isSafeURL(s string) bool {
	if protocol, _, ok := strings.Cut(s, ":"); ok && !strings.Contains(protocol, "/") {
		if !strings.EqualFold(protocol, "http") && !strings.EqualFold(protocol, "https") && !strings.EqualFold(protocol, "mailto") {
			return false
		}
	}
	return true
}
```

**File:** src/html/template/escape.go (L171-201)
```go
// escapeAction escapes an action template node.
func (e *escaper) escapeAction(c context, n *parse.ActionNode) context {
	if len(n.Pipe.Decl) != 0 {
		// A local variable assignment, not an interpolation.
		return c
	}
	c = nudge(c)
	// Check for disallowed use of predefined escapers in the pipeline.
	for pos, idNode := range n.Pipe.Cmds {
		node, ok := idNode.Args[0].(*parse.IdentifierNode)
		if !ok {
			// A predefined escaper "esc" will never be found as an identifier in a
			// Chain or Field node, since:
			// - "esc.x ..." is invalid, since predefined escapers return strings, and
			//   strings do not have methods, keys or fields.
			// - "... .esc" is invalid, since predefined escapers are global functions,
			//   not methods or fields of any types.
			// Therefore, it is safe to ignore these two node types.
			continue
		}
		ident := node.Ident
		if _, ok := predefinedEscapers[ident]; ok {
			if pos < len(n.Pipe.Cmds)-1 ||
				c.state == stateAttr && c.delim == delimSpaceOrTagEnd && ident == "html" {
				return context{
					state: stateError,
					err:   errorf(ErrPredefinedEscaper, n, n.Line, "predefined escaper %q disallowed in template", ident),
				}
			}
		}
	}
```
