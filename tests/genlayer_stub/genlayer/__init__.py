"""
Minimal offline stub of the `genlayer` SDK.

THIS IS A TEST-ONLY SHIM, NOT PART OF THE DEPLOYABLE CONTRACTS.

This starts from the same stub used (and reviewed) on the sibling
MatchGuard project - same Address/TreeMap/u256/UserError/gl.public/
gl.message/gl.eq_principle shapes - and adds two things MatchGuard
never needed because it was a single, standalone contract that never
moved GEN:

  1. `gl.get_contract_at(address)` - a tiny in-memory contract
     registry so ShipGuard's three contracts can call each other
     exactly the way they will in production: `.view().method(...)`
     for reads, `.emit(on="accepted").method(...)` for triggering a
     write on another contract (the caller's address is presented to
     the callee as whichever contract is currently executing, not the
     original transaction sender), and a bare
     `.emit(value=amount, on="accepted")` for a native-value payout
     with no method attached.
  2. `gl.message.value` plus a tiny in-memory ledger, so payable calls
     and payouts can be exercised and their balances asserted in
     tests, without a real chain.

Like the sibling stub, this does NOT attempt to simulate real network
access, real LLM behavior, multi-validator consensus/leader rotation,
or GenVM's real deterministic clock (this stub uses the host machine's
system clock via `datetime.datetime.now`, which is fine since tests
only assert relative ordering/behavior, never an exact wall-clock
value). Those require the actual GenLayer Studio/testnet - see
README.md "Known areas to verify".
"""

__all__ = ["gl", "TreeMap", "u256", "DynArray", "i256", "bigint", "Address", "Ledger"]


class _SubscriptableContainer:
    """Base for storage-type stand-ins that support `Type[K, V]` syntax
    used in class-level annotations (e.g. `TreeMap[str, str]`)."""

    def __class_getitem__(cls, item):
        return cls


class TreeMap(_SubscriptableContainer, dict):
    """Stand-in for genlayer's persistent TreeMap - behaves like a dict."""


class DynArray(_SubscriptableContainer, list):
    """Stand-in for genlayer's persistent DynArray - behaves like a list."""


class u256(int):
    """Stand-in for genlayer's fixed-width unsigned integer type."""


class i256(int):
    """Stand-in for genlayer's fixed-width signed integer type."""


class bigint(int):
    """Stand-in for genlayer's arbitrary-precision integer type."""


class Address:
    """
    Minimal stand-in for genlayer's Address type: validates a
    40-hex-character, "0x"-prefixed string and compares/hashes
    case-insensitively, matching how EVM-style addresses behave.
    """

    def __init__(self, value):
        text = str(value)
        if not text.startswith("0x") or len(text) != 42:
            raise ValueError(f"invalid address: {text!r}")
        int(text[2:], 16)  # raises ValueError if not valid hex
        self._value = text

    def __str__(self):
        return self._value

    def __repr__(self):
        return f"Address({self._value!r})"

    def __eq__(self, other):
        return str(self).lower() == str(other).lower()

    def __hash__(self):
        return hash(str(self).lower())


class UserError(Exception):
    """Stand-in for genlayer.gl.vm.UserError."""


class _Vm:
    """Stand-in for `gl.vm` - exposes UserError at its real SDK path."""

    UserError = UserError


class _WriteDecorator:
    """Stand-in for `gl.public.write`. Callable directly (plain write
    method) and also exposes `.payable` for methods that receive
    attached native value - both are no-ops here, matching how
    `gl.public.view` is already handled."""

    def __call__(self, fn):
        return fn

    @staticmethod
    def payable(fn):
        return fn


class _PublicNamespace:
    """Stand-in for `gl.public` - decorators are no-ops that just mark
    a method as a plain callable (no ABI/consensus wiring needed for
    unit tests of internal logic)."""

    write = _WriteDecorator()

    @staticmethod
    def view(fn):
        return fn


class _NondetWeb:
    """Stand-in for `gl.nondet.web`. `render` raises by default; tests
    monkeypatch this with `unittest.mock.patch` to simulate specific
    fetch outcomes (success, timeout, empty page, garbage content...)."""

    @staticmethod
    def render(url, mode="text"):
        raise NotImplementedError("gl.nondet.web.render must be patched in tests")


class _Nondet:
    web = _NondetWeb()

    @staticmethod
    def exec_prompt(prompt, response_format="text"):
        raise NotImplementedError("gl.nondet.exec_prompt must be patched in tests")


class _EqPrinciple:
    """
    Stand-in for `gl.eq_principle`.

    ShipGuard's evidence pipeline uses `prompt_comparative` (never
    `strict_eq`) per GenLayer's documented guidance that strict_eq
    must never be used for LLM-derived output. For offline unit tests
    we simply run `fn` once and return its result; simulating the
    actual NLP comparator (or real multi-validator consensus at all)
    requires the live GenLayer Studio/testnet.
    """

    @staticmethod
    def strict_eq(fn):
        return fn()

    @staticmethod
    def prompt_comparative(fn, principle=None):
        return fn()

    @staticmethod
    def prompt_non_comparative(fn, task="", criteria=""):
        return fn()


class _Message:
    """
    Stand-in for `gl.message`. `sender_address` and `value` are plain
    mutable attributes here (in the real SDK they're derived from the
    actual signed transaction) - tests set them directly before each
    call to simulate a specific caller / attached value, e.g.:

        gl.message.sender_address = Address("0x" + "11" * 20)
        gl.message.value = u256(0)
    """

    sender_address = None
    value = u256(0)


class Ledger:
    """
    Tiny in-memory GEN ledger so tests can assert on balances after
    payable calls and payouts, mirroring how real value would move on
    chain. Not part of the real SDK - a stub-only convenience.
    """

    _balances = {}

    @classmethod
    def reset(cls):
        cls._balances = {}

    @classmethod
    def fund(cls, address, amount) -> None:
        key = str(address).lower()
        cls._balances[key] = cls._balances.get(key, 0) + int(amount)

    @classmethod
    def balance_of(cls, address) -> int:
        return cls._balances.get(str(address).lower(), 0)

    @classmethod
    def transfer(cls, from_address, to_address, amount) -> None:
        amount = int(amount)
        if amount <= 0:
            return
        from_key = str(from_address).lower()
        if cls._balances.get(from_key, 0) < amount:
            raise UserError(
                f"insufficient balance: {from_address} has {cls._balances.get(from_key, 0)}, "
                f"needs {amount}"
            )
        cls._balances[from_key] = cls._balances.get(from_key, 0) - amount
        cls._balances[str(to_address).lower()] = cls._balances.get(str(to_address).lower(), 0) + amount


class _CallStack:
    """Tracks which registered contract's method is currently
    executing, so an outgoing cross-contract call can present that
    contract's own address as the sender to the callee - not the
    original transaction's sender."""

    _stack = []

    @classmethod
    def push(cls, address) -> None:
        cls._stack.append(address)

    @classmethod
    def pop(cls) -> None:
        cls._stack.pop()

    @classmethod
    def current(cls):
        return cls._stack[-1] if cls._stack else None


class _ContractRegistry:
    _by_address = {}

    @classmethod
    def reset(cls):
        cls._by_address = {}

    @classmethod
    def register(cls, address: Address, instance) -> None:
        cls._by_address[str(address).lower()] = instance

    @classmethod
    def get(cls, address):
        instance = cls._by_address.get(str(address).lower())
        if instance is None:
            raise UserError(f"no contract registered at {address} in this test run.")
        return instance


def register_contract(address, instance):
    """
    Test-bootstrap helper: assigns `instance` its on-chain `address`
    for the purposes of this stub, and wraps every public-looking
    method so that any outgoing cross-contract call made from inside
    one of those methods presents THIS contract's address as the
    caller (see _CallStack above).
    """
    addr = Address(str(address))
    for name in list(vars(type(instance)).keys()) + list(vars(instance).keys()):
        if name.startswith("_"):
            continue
        attr = getattr(instance, name, None)
        if not callable(attr):
            continue
        object.__setattr__(instance, name, _wrap_with_context(attr, addr))
    _ContractRegistry.register(addr, instance)
    return instance


def _wrap_with_context(fn, address):
    def wrapper(*args, **kwargs):
        is_top_level = _CallStack.current() is None
        if is_top_level and int(gl.message.value) > 0:
            # Attached value on a top-level call materializes into the
            # target contract's own balance, exactly as real attached
            # value would land in the called contract on chain.
            Ledger.fund(address, gl.message.value)
        _CallStack.push(address)
        try:
            return fn(*args, **kwargs)
        finally:
            _CallStack.pop()

    return wrapper


class _MethodProxy:
    def __init__(self, address, is_write):
        self._address = address
        self._is_write = is_write

    def __getattr__(self, name):
        def call(*args, **kwargs):
            target = _ContractRegistry.get(self._address)
            method = getattr(target, name)
            if not self._is_write:
                return method(*args, **kwargs)

            caller_address = _CallStack.current()
            previous_sender = gl.message.sender_address
            if caller_address is not None:
                gl.message.sender_address = caller_address
            try:
                return method(*args, **kwargs)
            finally:
                gl.message.sender_address = previous_sender

        return call


class _ContractProxy:
    def __init__(self, address):
        self._address = address

    def view(self):
        return _MethodProxy(self._address, is_write=False)

    def emit(self, on=None, value=None, **kwargs):
        if value is not None and int(value) > 0:
            caller_address = _CallStack.current()
            if caller_address is None:
                raise UserError(
                    "emit(value=...) called with no contract currently executing - "
                    "this stub can only attribute a payout to whichever registered "
                    "contract's method is on the call stack."
                )
            Ledger.transfer(caller_address, self._address, value)
        return _MethodProxy(self._address, is_write=True)


class _Contract:
    """
    Stand-in base class for `gl.Contract`.

    In real GenVM, fields declared with persistent storage types
    (TreeMap, DynArray, ...) are automatically backed by chain state
    and start out empty - contracts are not expected to initialize
    them by hand in `__init__`. This stub reproduces that by scanning
    class annotations at construction time and pre-populating any
    TreeMap/DynArray fields with empty instances before the contract's
    own `__init__` runs.
    """

    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        for klass in reversed(cls.__mro__):
            for name, annotation in vars(klass).get("__annotations__", {}).items():
                if isinstance(annotation, type) and issubclass(annotation, (TreeMap, DynArray)):
                    setattr(instance, name, annotation())
        return instance


class _GL:
    Contract = _Contract
    public = _PublicNamespace()
    nondet = _Nondet()
    eq_principle = _EqPrinciple()
    vm = _Vm()
    message = _Message()

    @staticmethod
    def get_contract_at(address):
        return _ContractProxy(address)


gl = _GL()
