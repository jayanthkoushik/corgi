from __future__ import annotations

import argparse
import importlib
import sys
from collections import defaultdict
from collections.abc import Sequence as AbstractSequence
from contextlib import suppress
from functools import partial
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    IO,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Type,
    TypeVar,
    Union,
)

if sys.version_info >= (3, 9):
    from typing import get_type_hints, Literal
else:
    from typing_extensions import get_type_hints, Literal

from ._helpfmt import CorgyHelpFormatter
from ._utils import (
    check_val_type,
    get_concrete_collection_type,
    is_literal_type,
    is_optional_type,
    OptionalTypeAction,
)

# The main interface is the `Corgy` class. `_CorgyMeta` modifies creation of `Corgy`
# (and its subclasses) by converting annotations to properties, and setting up utilities
# for command line parsing. `corgyparser` is a decorator that allows custom parsers to
# be defined for `Corgy` attributes.

__all__ = ("Corgy", "corgyparser")
_T = TypeVar("_T", bound="Corgy")


class BooleanOptionalAction(argparse.Action):
    # :meta private:
    # Backport of `argparse.BooleanOptionalAction` from Python 3.9.
    # Taken almost verbatim from `CPython/Lib/argparse.py`.
    def __init__(self, option_strings, dest, *args, **kwargs):
        _option_strings = []
        for option_string in option_strings:
            _option_strings.append(option_string)

            if option_string.startswith("--"):
                option_string = "--no-" + option_string[2:]
                _option_strings.append(option_string)

        super().__init__(
            option_strings=_option_strings, dest=dest, nargs=0, *args, **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):
        if option_string in self.option_strings:
            setattr(namespace, self.dest, not option_string.startswith("--no-"))


class _CorgyMeta(type):
    """Metaclass for `Corgy`.

    Modifies class creation by parsing type annotations, and creating properties for
    each annotated attribute. Default values and custom parsers are stored in the
    `__defaults` and `__parsers` attributes. Custom flags, if present, are stored in
    the `__flags` attribute.
    """

    __slots__ = ()

    def __new__(cls, name, bases, namespace, **kwds) -> _CorgyMeta:
        try:
            _make_slots = kwds.pop("corgy_make_slots")
        except KeyError:
            _make_slots = True

        if _make_slots:
            if "__slots__" not in namespace:
                namespace["__slots__"] = []
            else:
                namespace["__slots__"] = list(namespace["__slots__"])
        elif "__slots__" in namespace:
            raise TypeError(
                "`__slots__` cannot be defined if `corgy_make_slots` is `False`"
            )

        cls_annotations = namespace.get("__annotations__", {})
        namespace["__annotations__"] = {}
        namespace["__defaults"] = {}
        namespace["__flags"] = {}
        namespace["__parsers"] = {}
        namespace["__helps"] = {}

        # See if `corgy_track_bases` is specified (default `True`).
        try:
            _track_bases = kwds.pop("corgy_track_bases")
        except KeyError:
            _track_bases = True
        if _track_bases:
            for base in bases:
                _base_annotations = getattr(base, "__annotations__", {})
                namespace["__annotations__"].update(_base_annotations)
                if isinstance(base, cls):
                    # `base` is also a `Corgy` class.
                    namespace["__defaults"].update(getattr(base, "__defaults"))
                    namespace["__flags"].update(getattr(base, "__flags"))
                    namespace["__parsers"].update(getattr(base, "__parsers"))
                    namespace["__helps"].update(getattr(base, "__helps"))
                else:
                    # Fetch default values directly from the base.
                    for _var_name in _base_annotations:
                        try:
                            namespace["__defaults"][_var_name] = getattr(
                                base, _var_name
                            )
                        except AttributeError:
                            pass

        # Add current annotations last, so that they override base values.
        namespace["__annotations__"].update(cls_annotations)

        tempnew = super().__new__(cls, name, bases, namespace)
        type_hints = get_type_hints(tempnew, include_extras=True)

        if not type_hints:
            return tempnew

        del tempnew  # YUCK
        for var_name in set(namespace["__annotations__"].keys()):
            var_ano = type_hints[var_name]
            # Check for name conflicts.
            _mangled_name = f"_{name.lstrip('_')}__{var_name}"
            if _mangled_name in namespace or _mangled_name in cls_annotations:
                raise TypeError(f"name clash: `{var_name}`, `{_mangled_name}`")

            if hasattr(var_ano, "__origin__") and hasattr(var_ano, "__metadata__"):
                # `<var_name>: Annotated[<var_type>, <var_help>, [<var_flags>]]`.
                var_type = var_ano.__origin__
                var_help = var_ano.__metadata__[0]
                if not isinstance(var_help, str):
                    raise TypeError(
                        f"incorrect help string annotation for variable `{var_name}`: "
                        f"expected str"
                    )

                if len(var_ano.__metadata__) > 1:
                    if isinstance(var_type, cls):
                        # Custom flags are not allowed for groups.
                        raise TypeError(
                            f"invalid annotation for group `{var_name}`: "
                            f"custom flags not allowed for groups"
                        )

                    var_flags = var_ano.__metadata__[1]
                    if not isinstance(var_flags, list) or not var_flags:
                        raise TypeError(
                            f"incorrect custom flags for variable `{var_name}`: "
                            f"expected non-empty list"
                        )
                else:
                    var_flags = None
            else:
                # `<var_name>: <var_type>`.
                var_type = var_ano
                var_help = namespace["__helps"].get(var_name, None)
                var_flags = namespace["__flags"].get(var_name, None)

            if hasattr(var_type, "__origin__") and var_type.__origin__ is ClassVar:
                # Class variable: make sure it has an associated value.
                if var_name not in namespace:
                    if var_name in namespace["__defaults"]:
                        del namespace["__defaults"][var_name]
                    else:
                        raise TypeError(f"class variable `{var_name}` has no value set")
                del namespace["__annotations__"][var_name]
                continue

            namespace["__annotations__"][var_name] = var_type

            if var_help is not None:
                namespace["__helps"][var_name] = var_help
            if var_flags is not None:
                namespace["__flags"][var_name] = var_flags

            # Add default value to dedicated dict.
            if var_name in namespace:
                try:
                    check_val_type(namespace[var_name], var_type)
                except ValueError as e:
                    raise ValueError(
                        f"default value type mismatch for '{var_name}'"
                    ) from e
                namespace["__defaults"][var_name] = namespace[var_name]
            elif var_name in namespace["__defaults"] and var_name in cls_annotations:
                # Variable had a default value in a base class, but does not anymore.
                del namespace["__defaults"][var_name]

            # Create `<var_name>` property.
            namespace[var_name] = cls._create_var_property(
                name, var_name, var_type, var_help
            )
            if _make_slots:
                if f"__{var_name}" in namespace["__slots__"]:
                    raise TypeError(
                        f"cannot have slot `__{var_name}`: internal clash with "
                        f"`{var_name}`"
                    )
                namespace["__slots__"].append(f"__{var_name}")

        if _make_slots:
            namespace["__slots__"] = tuple(namespace["__slots__"])

        # Store custom parsers in a dict.
        for _, v in namespace.items():
            if not isinstance(v, _CorgyParser):
                continue
            for var_name in v.var_names:
                if (var_name in namespace["__annotations__"]) and isinstance(
                    namespace[var_name], property
                ):
                    namespace["__parsers"][var_name] = v.fparse
                else:
                    raise TypeError(f"invalid target for corgyparser: {var_name}")

        return super().__new__(cls, name, bases, namespace, **kwds)

    @staticmethod
    def _create_var_property(cls_name, var_name, var_type, var_doc) -> property:
        # Properties are stored in private instance variables prefixed with `__`, and
        # must be accessed as `_<cls>__<var_name>`.
        def var_fget(self):
            with suppress(AttributeError):
                return getattr(self, f"_{cls_name.lstrip('_')}__{var_name}")
            # with suppress(KeyError):
            #     return getattr(self, "__defaults")[var_name]
            raise AttributeError(f"no value available for attribute `{var_name}`")

        def var_fset(self, val):
            check_val_type(val, var_type)
            setattr(self, f"_{cls_name.lstrip('_')}__{var_name}", val)

        def var_fdel(self):
            delattr(self, f"_{cls_name.lstrip('_')}__{var_name}")

        var_fget.__annotations__ = {"return": var_type}
        var_fset.__annotations__ = {"val": var_type}

        return property(var_fget, var_fset, var_fdel, doc=var_doc)


class Corgy(metaclass=_CorgyMeta):
    """Base class for collections of attributes.

    To use, subclass `Corgy`, and declare attributes using type annotations::

        >>> from corgy import Corgy

        >>> class A(Corgy):
        ...     x: int
        ...     y: float

    At runtime, class `A` will have `x`, and `y` as properties, so that the class can be
    used similar to Python dataclasses::

        >>> a = A()
        >>> a.x = 1
        >>> a.x
        1

        >>> a.y
        Traceback (most recent call last):
           ...
        AttributeError: no value available for attribute `y`

        >>> a.y = a.x + 1.1
        >>> a.y
        2.1

        >>> del a.x  # unset x
        >>> a.x
        Traceback (most recent call last):
           ...
        AttributeError: no value available for attribute `x`

    Note that the class's `__init__` method only accepts keyword arguments, and ignores
    arguments without a corresponding attribute. The following are all valid::

        >>> A(x=1, y=2.1)
        A(x=1, y=2.1)

        >>> A(x=1, z=3)  # y is not set, and z is ignored
        A(x=1)

        >>> A(**{"x": 1, "y": 2.1, "z": 3})
        A(x=1, y=2.1)

    Attribute values are type-checked, and `ValueError` is raised on type mismatch::

        >>> a = A(x="1")
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type '<class 'int'>': '1'

        >>> a = A()
        >>> a.x = "1"
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type '<class 'int'>': '1'

        >>> class A(Corgy):
        ...     x: int = "1"
        Traceback (most recent call last):
            ...
        ValueError: default value type mismatch for 'x'

    Any type which supports type checking with `isinstance` can be used as an
    attribute type (along with some special type annotations that are discussed below).
    This includes other corgy classes::

        >>> class A(Corgy):
        ...     x: int
        ...     y: float

        >>> class B(Corgy):
        ...     x: int
        ...     a: A

        >>> b = B(x=1)
        >>> b.a = A()
        >>> b.a.x = 10
        >>> b
        B(x=1, a=A(x=10))

    `Corgy` classes have their `__slots__` set to the annotated attributes. So, if you
    want to use additional attributes not tracked by `Corgy`, define them (and only
    them) in `__slots__`::

        >>> class A(Corgy):
        ...     __slots__ = ("x",)
        ...     y: int

        >>> a = A()
        >>> a.y = 1  # `Corgy` attribute
        >>> a.x = 2  # custom attribute
        >>> a
        A(y=1)

    To allow arbitrary instance attributes, add `__dict__` to `__slots__`. Names added
    through custom `__slots__` are not processed by `Corgy`. Alternatively, to disable
    setting `__slots__` completely, set `corgy_make_slots` to `False` in the class
    definition::

        >>> class A(Corgy, corgy_make_slots=False):
        ...     y: int

        >>> a = A()
        >>> a.y = 1  # `Corgy` attribute
        >>> a.x = 2  # custom attribute
        >>> a
        A(y=1)

    Names marked with the `ClassVar` type will be added as class variables, and will
    not be available as `Corgy` attributes::

        >>> from typing import ClassVar

        >>> class A(Corgy):
        ...     x: ClassVar[int] = 3

        >>> A.x
        3
        >>> A.x = 4
        >>> A.x
        4
        >>> a = A()
        >>> a.x
        4
        >>> a.x = 5
        Traceback (most recent call last):
            ...
        AttributeError: 'A' object attribute 'x' is read-only

    Also note that class variables need to be assigned to a value during definition, and
    this value will not be type checked by `Corgy`.

    Inheritance works as expected, whether base classes are themselves `Corgy` classes
    or not, with sub-classes inheriting the attributes of the base class, and overriding
    any redefined attributes::

        >>> class A:
        ...     x: int

        >>> class B(Corgy, A):
        ...     y: float = 1.0
        ...     z: str

        >>> class C(B):
        ...     y: float = 2.0
        ...     z: str
        ...     w: float

        >>> c = C()
        >>> print(c)
        C(x=<unset>, y=2.0, z=<unset>, w=<unset>)

    Tracking of base class attributes can be disabled by setting `corgy_track_bases` to
    `False` in the class definition. Properties will still be inherited following
    standard inheritance rules, but `Corgy` will ignore them::

        >>> class A:
        ...     x: int

        >>> class B(Corgy, A, corgy_track_bases=False):
        ...     y: float = 1.0
        ...     z: str

        >>> b = B()
        >>> print(b)
        B(y=1.0, z=<unset>)

    `Corgy` recognizes a number of special annotations, which are used to control how
    attribute values are processed.

    Note:
        If any of the following annotations are unavailable in the Python version being
        used, you can import them from `typing_extensions` (which is available on PyPI).

    *Annotations*
    `typing.Annotated` can be used to add additional metadata to attributes, akin to
    doc strings. It is primarily used to control how attributes are added to
    `ArgumentParser` instances. `typing.Annotated` is stripped on class creation,
    leaving only the base type::

        >>> import sys
        >>> if sys.version_info >= (3, 9):
        ...     from typing import Annotated, Literal
        ... else:
        ...     from typing_extensions import Annotated, Literal

        >>> class A(Corgy):
        ...     x: Annotated[int, "this is x"]

        >>> A.attrs()
        {'x': <class 'int'>}

    Refer to the docs for `Corgy.add_args_to_parser` for details on how to use this
    annotation.

    *Optional*
    Annotating an attribute with `typing.Optional` allows it to be `None`::

        >>> from typing import Optional

        >>> class A(Corgy):
        ...     x: Optional[int]

        >>> a = A()
        >>> a.x = None

    In Python >= 3.10, instead of using `typing.Annotated`, `| None` can be used, i.e.,
    `x: int | None` for example.

    *Collections*
    Several collection types can be used to annotate attributes, which will restrict the
    type of accepted values. Values in the collection will be checked to ensure that
    they match the annotated collection types. The following collection types are
    supported:

    1. `collections.abc.Sequence` (`typing.Sequence` on Python < 3.9)
    2. `tuple` (`typing.Tuple` on Python < 3.9)
    3. `list` (`typing.List` on Python < 3.9)
    4. `set` (`typing.Set` on Python < 3.9)

    There are a few different ways to use these types, each resulting in different
    validation conditions. The simplest case is a plain (possibly empty) collection of a
    single type::

        >>> from typing import List, Sequence, Set, Tuple

        >>> class A(Corgy):
        ...     x: Sequence[int]
        ...     y: Tuple[str]
        ...     z: Set[float]
        ...     w: List[int]

        >>> a = A()
        >>> a.x = [1, 2]
        >>> a.y = ("1", "2")
        >>> a.z = {1.0, 2.0}
        >>> a.w = [1, 2]
        >>> a
        A(x=[1, 2], y=('1', '2'), z={1.0, 2.0}, w=[1, 2])

        >>> a.x = [1, "2"]
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type '<class 'int'>': '2'

        >>> a.x = (1, 2)      # `Sequence` accepts any sequence type

        >>> a.y = ["1", "2"]  # `Tuple` only accepts tuples
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type 'typing.Tuple[str]': ['1', '2']

    The collection length can be controlled by the arguments of the type annotation.
    Note, however, that `typing.Sequence/typing.List/typing.Set` do not
    accept multiple arguments, and so, cannot be used if collection length has to be
    specified. On Python < 3.9, only `typing.Tuple` can be used for controlling
    collection lengths.

    To specify that a collection must be non-empty, use ellipsis (`...`) as the second
    argument of the type::

        >>> class A(Corgy):
        ...     x: Tuple[int, ...]

        >>> a = A()
        >>> a.x = tuple()
        Traceback (most recent call last):
            ...
        ValueError: expected non-empty collection for type 'typing.Tuple[int, ...]'

    Collections can also be restricted to be of a fixed length::

        >>> class A(Corgy):
        ...     x: Tuple[int, str]
        ...     y: Tuple[int, int, int]

        >>> a = A()
        >>> a.x = (1, 1)
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type '<class 'str'>': 1

        >>> a.y = (1, 1)  # doctest: +NORMALIZE_WHITESPACE
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type 'typing.Tuple[int, int, int]': (1, 1):
        expected exactly '3' elements

    *Literals*
    `typing.Literal` can be used to specify that an attribute takes one of a fixed set
    of values::

        >>> class A(Corgy):
        ...     x: Literal[0, 1, "2"]

        >>> a = A()
        >>> a.x = 0
        >>> a.x = "2"
        >>> a.x = "1"  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type 'typing.Literal[0, 1, '2']': '1'

    Type annotations can be nested; for instance,
    `Sequence[Literal[0, 1, 2], Literal[0, 1, 2]]` represents a sequence of length 2,
    where each element is either 0, 1, or 2.

    A fixed set of attribute values can also be specified by adding a `__choices__`
    attribute to the argument type, containing a collection of choices::

        >>> class T(int):
        ...     __choices__ = (1, 2)

        >>> class A(Corgy):
        ...     x: T

        >>> a = A()
        >>> a.x = 1
        >>> a.x = 3  # doctest: +NORMALIZE_WHITESPACE
        Traceback (most recent call last):
            ...
        ValueError: invalid value for type '<class 'T'>': 3:
        expected one of: (1, 2)

    Note that choices specified in this way are not type-checked to ensure that they
    match the argument type; in the above example, `__choices__` could be set to
    `(1, "2")`.
    """

    @classmethod
    def add_args_to_parser(
        cls,
        parser: argparse._ActionsContainer,
        name_prefix: str = "",
        flatten_subgrps: bool = False,
        defaults: Optional[Mapping[str, Any]] = None,
    ):
        """Add the class' `Corgy` attributes to the given parser.

        Args:
            parser: Argument parser/group to which the attributes will be added.
            name_prefix: Prefix for argument names. Arguments will be named
                `--<name-prefix>:<attr-name>`. If custom flags are present,
                `--<name-prefix>:<flag>` will be used instead (one for each flag).
            flatten_subgrps: Whether to add sub-groups to the main parser instead of
                creating argument groups. Note: sub-sub-groups are always added with
                this argument set to `True`, since `argparse` in unable to properly
                display nested group arguments.
            defaults: Optional mapping with default values for arguments. Any value
                specified here will override default values specified in the class.
                Values for groups can be specified either as `Corgy` instances, or as
                individual values using the same syntax as for `Corgy.from_dict`.

        Type annotations control how attributes are added to the parser. A number of
        special annotations are parsed and stripped from attribute types to determine
        the parameters for calling `ArgumentParser.add_argument`. These special
        annotations are described below.

        *Annotated*
        `typing.Annotated` can be used to add a help message for the argument::

            >>> from argparse import ArgumentParser, SUPPRESS
            >>> from corgy import CorgyHelpFormatter

            >>> class A(Corgy):
            ...     x: Annotated[int, "help for x"]

            >>> parser = ArgumentParser(
            ...     formatter_class=CorgyHelpFormatter,
            ...     add_help=False,
            ...     usage=SUPPRESS,
            ... )

            >>> A.add_args_to_parser(parser)
            >>> parser.print_help()
            options:
              --x int  help for x (required)

        This annotation can also be used to modify the parser flags for the argument. By
        default, the attribute name is used, prefixed with `--`, and with `_` replaced
        by `-`. If the custom flag does not have a leading `-`, a positional argument
        will be created::

            >>> class A(Corgy):
            ...     x: Annotated[int, "help for x", ["-x", "--ex"]]
            ...     y: Annotated[int, "help for y", ["y"]]

            >>> parser = ArgumentParser(
            ...     formatter_class=CorgyHelpFormatter,
            ...     add_help=False,
            ...     usage=SUPPRESS,
            ... )

            >>> A.add_args_to_parser(parser)
            >>> parser.print_help()
            positional arguments:
              y int        help for y
            <BLANKLINE>
            options:
              -x/--ex int  help for x (required)

        `Annotated` can accept multiple arguments, but only the first three are used
        by `Corgy`. The first argument is the attribute type, the second is the help
        message (which must be a string), and the third is a sequence of flags.

        Note:
            `Annotated` should always be the outermost annotation for an attribute.

        *Optional*
        Attributes marked with `typing.Optional` are allowed to be `None`. The
        arguments for these attributes can be passed with no values (i.e. `--x`
        instead of `--x=1` or `--x 1`) to indicate that the value should be `None`.

        Note: Attributes with default values are also "optional" in the sense that
        they can be omitted from the command line. However, they are not the same as
        attributes marked with `Optional`, since the former are not allowed to be
        `None`. Furthermore, required `Optional` attributes without default values
        _will_ need to be passed on the command line (possibly with no values).

        *Boolean*
        `bool` types (when not in a collection) are converted to a pair of options::

            >>> class A(Corgy):
            ...     arg: bool

            >>> parser = ArgumentParser(
            ...     formatter_class=CorgyHelpFormatter,
            ...     add_help=False,
            ...     usage=SUPPRESS,
            ... )

            >>> A.add_args_to_parser(parser)
            >>> parser.print_help()
            options:
              --arg/--no-arg  (required)

        *Collection*
        Collection types are added to the parser by setting `nargs`. The value for
        `nargs` is determined by the collection type. Plain collections, such as
        `Sequence[int]`, will be added with `nargs=*`; Non-empty collections, such as
        `Sequence[int, ...]`, will be added with `nargs=+`; Finally, fixed-length
        collections, such as `Sequence[int, int, int]`, will be added with `nargs` set
        to the length of the collection.

        In all cases, collection types can only be added to a parser if they are single
        type. Heterogenous collections, such as `Sequence[int, str]` cannot be added,
        and will raise `ValueError`. Untyped collections (e.g., `x: Sequence`), also
        cannot be added.

        Arguments for optional collections will also accept no values to indicate
        `None`. Due to this, it is not possible to parse an empty collection for
        an optional collection argument::

            >>> class A(Corgy):
            ...     x: Optional[Sequence[int]]
            ...     y: Sequence[int]

            >>> parser = ArgumentParser()
            >>> A.add_args_to_parser(parser)
            >>> parser.parse_args(["--x", "--y"])
            Namespace(x=None, y=[])

        *Literal*
        For `Literal` types, the provided values are passed to the `choices` argument
        of `ArgumentParser.add_argument`. All values must be of the same type, which
        will be inferred from the type of the first value. If the first value has a
        `__bases__` attribute, the type will be inferred as the first base type, and
        all other choices must be subclasses of that type::

            >>> class T: ...
            >>> class T1(T): ...
            >>> class T2(T): ...

            >>> class A(Corgy):
            ...     x: Literal[T1, T2]

            >>> parser = ArgumentParser(
            ...     formatter_class=CorgyHelpFormatter,
            ...     add_help=False,
            ...     usage=SUPPRESS,
            ... )

            >>> A.add_args_to_parser(parser)
            >>> parser.print_help()
            options:
              --x T  ({T1/T2} required)

        For types which specify choices by defining `__choices__`, the values are
        passed to the `choices` argument as with `Literal`, but no type inference is
        performed, and the base attribute type will be used as the argument type.

        *Corgy*
        Attributes which are themselves `Corgy` types are treated as argument groups.
        Group arguments are added to the command line parser with the group attribute
        name prefixed. Note that groups will ignore any custom flags when computing the
        prefix; elements within the group will use custom flags, but because they are
        prefixed with `--`, they will not be positional.

        Example::

            >>> class G(Corgy):
            ...     x: int = 0
            ...     y: float

            >>> class A(Corgy):
            ...     x: int
            ...     g: G

            >>> parser = ArgumentParser(
            ...     formatter_class=CorgyHelpFormatter,
            ...     add_help=False,
            ...     usage=SUPPRESS,
            ... )

            >>> A.add_args_to_parser(parser)
            >>> parser.print_help()
            options:
              --x int      (required)
            <BLANKLINE>
            g:
              --g:x int    (default: 0)
              --g:y float  (required)

        **Custom parsers**

        Attributes for which a custom parser is defined using `@corgyparser` will
        be added with a custom action that will call the parser. Refer to the
        documentation for `corgyparser` for details.

        **Metavar**

        This function will not explicitly pass a value for the `metavar` argument of
        `ArgumentParser.add_argument`, unless an attribute's type defines `__metavar__`,
        in which case, it will be passed as is. To change the metavar for attributes
        with custom parsers, set the `metavar` argument of `corgyparser`.
        """
        base_parser = parser
        base_defaults = getattr(cls, "__defaults").copy()
        if defaults is not None:
            base_defaults.update(defaults)

        # Extract default values for group arguments specified individually using
        # the `<group>:<var name>` syntax.
        group_arg_defaults: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for _k, _v in base_defaults.items():
            if ":" in _k:
                _grp_name, _var_name = _k.split(":")
                group_arg_defaults[_grp_name][_var_name] = _v
            elif _k not in cls.__annotations__:
                raise ValueError(f"default value for unknown argument: `{_k}`")

        for var_name, var_type in cls.attrs().items():
            var_flags = getattr(cls, "__flags").get(
                var_name, [f"--{var_name.replace('_', '-')}"]
            )
            if name_prefix:
                var_flags = [
                    f"--{name_prefix.replace('_', '-')}:{flag.lstrip('-')}"
                    for flag in var_flags
                ]
                var_dest = f"{name_prefix}:{var_name}"
            else:
                var_dest = var_name

            if not any(_flag.startswith("-") for _flag in var_flags):
                var_flags = [var_name]
                var_positional = True
            elif all(_flag.startswith("-") for _flag in var_flags):
                var_positional = False
            else:
                raise TypeError(
                    f"inconsistent positional/optional flags for {var_name}: "
                    f"{var_flags}"
                )

            var_help = getattr(cls, var_name).__doc__  # doc is stored in the property

            # Check if the variable is also `Corgy` type.
            if type(var_type) is type(cls):
                # Create an argument group using `<var_type>`.
                # If there is a default value, pass it using `**defaults`.
                if var_name in base_defaults:
                    try:
                        grp_defaults = base_defaults[var_name].as_dict()
                    except AttributeError:
                        raise ValueError(
                            f"default value for `{var_name}` is not a `Corgy` instance"
                        ) from None
                else:
                    grp_defaults = {}

                # Update defaults with any values specified individually.
                grp_defaults.update(group_arg_defaults.get(var_name, {}))

                grp_parser: argparse._ActionsContainer
                if flatten_subgrps:
                    grp_parser = base_parser
                else:
                    grp_parser = base_parser.add_argument_group(var_dest, var_help)
                var_type.add_args_to_parser(grp_parser, var_dest, True, grp_defaults)
                continue

            var_action: Optional[Type[argparse.Action]] = None

            # Check if the variable is required on the command line.
            var_required = var_name not in base_defaults

            # Check if the variable can be `None`.
            if is_optional_type(var_type):
                var_base_type = var_type.__args__[0]
                var_action = OptionalTypeAction
            else:
                var_base_type = var_type

            # Check if the variable is a collection.
            var_nargs: Union[int, Literal["+", "*", "?"], None] = None
            _col_type = get_concrete_collection_type(var_base_type)
            if _col_type is not None:
                if (
                    not hasattr(var_base_type, "__args__")
                    or not var_base_type.__args__
                    or isinstance(var_base_type.__args__[0], TypeVar)
                ):
                    raise TypeError(
                        f"`{var_name}` is a collection, but has no type arguments: "
                        f"use `{var_base_type}[<types>]"
                    )
                if len(var_base_type.__args__) == 1:
                    var_nargs = "*"
                elif (
                    len(var_base_type.__args__) == 2
                    and var_base_type.__args__[1] is Ellipsis
                ):
                    # `...` is used to represent non-empty collections, e.g.,
                    # `Sequence[int, ...]`.
                    var_nargs = "+"
                else:
                    # Ensure single type.
                    if any(
                        _a != var_base_type.__args__[0]
                        for _a in var_base_type.__args__[1:]
                    ):
                        raise TypeError(
                            f"`{var_name}` has unsupported type `{var_base_type}`: only"
                            f" single-type collections are supported"
                        )
                    var_nargs = len(var_base_type.__args__)
                var_base_type = var_base_type.__args__[0]

            # Check if the variable has choices.
            if is_literal_type(var_base_type):
                # Determine if the first choice has `__bases__`, in which case
                # the first base class is the type for the argument.
                try:
                    c0_type = var_base_type.__args__[0].__bases__[0]
                except AttributeError:
                    c0_type = type(var_base_type.__args__[0])
                    f_check_type: Callable[[Any, Any], bool] = isinstance
                else:
                    f_check_type = issubclass

                # All choices must be of the same type.
                if any(
                    not f_check_type(_a, c0_type) for _a in var_base_type.__args__[1:]
                ):
                    raise TypeError(
                        f"choices for `{var_name}` not all of type `{c0_type}`: "
                        f"`{var_base_type.__args__}`"
                    )
                var_choices = var_base_type.__args__
                var_base_type = c0_type
            elif hasattr(var_base_type, "__choices__"):
                var_choices = var_base_type.__choices__
            else:
                var_choices = None

            var_type_metavar: Optional[str] = getattr(
                var_base_type, "__metavar__", None
            )

            # Check if the variable is boolean. Boolean variables are converted to
            # `--<var-name>`/`--no-<var-name>` arguments.
            if var_base_type is bool and var_nargs is None and var_action is None:
                var_action = BooleanOptionalAction

            # Check if the variable has a custom parser.
            _parsers = getattr(cls, "__parsers")
            if var_name in _parsers:
                _var_parser = _parsers[var_name]
                var_action = partial(CorgyParserAction, _var_parser)  # type: ignore
                var_add_type = str
                var_nargs = getattr(_var_parser, "__nargs__", None)
                _parser_metavar = getattr(_var_parser, "__metavar__", None)
                if _parser_metavar is not None:
                    var_type_metavar = _parser_metavar
            else:
                var_add_type = var_base_type

            # Add the variable to the parser.
            _kwargs: Dict[str, Any] = {}
            if var_name in getattr(cls, "__flags") and not var_positional:
                _kwargs["dest"] = var_dest
            if var_help is not None:
                _kwargs["help"] = var_help
            if var_nargs is not None:
                _kwargs["nargs"] = var_nargs
            if var_action is not None:
                _kwargs["action"] = var_action
            if var_choices is not None:
                _kwargs["choices"] = var_choices
            if var_name in base_defaults:
                _kwargs["default"] = base_defaults[var_name]
            if var_required and not var_positional:
                _kwargs["required"] = True
            if var_type_metavar is not None:
                _kwargs["metavar"] = var_type_metavar
            parser.add_argument(*var_flags, type=var_add_type, **_kwargs)

    def __init__(self, **args):
        if self.__class__ is Corgy:
            raise TypeError("`Corgy` is an abstract class and cannot be instantiated")

        cls_attrs = self.attrs()
        cls_defaults = getattr(self, "__defaults")
        for attr_name in cls_attrs:
            if attr_name in args:
                setattr(self, attr_name, args[attr_name])
            elif attr_name in cls_defaults:
                setattr(self, attr_name, cls_defaults[attr_name])

    def _str(self, f_str: Callable[..., str]) -> str:
        s = f"{self.__class__.__name__}("
        i = 0
        for arg_name in self.attrs():
            try:
                _val_s = f_str(getattr(self, arg_name))
            except AttributeError:
                if f_str is repr:
                    continue
                _val_s = "<unset>"
            if i != 0:
                s = s + ", "
            s = s + f"{arg_name}="
            s = s + _val_s
            i += 1
        s = s + ")"
        return s

    def __repr__(self) -> str:
        return self._str(repr)

    def __str__(self) -> str:
        return self._str(str)

    def __eq__(self, other) -> bool:
        if other is self:
            return True
        if self.__class__ is not other.__class__:
            return False
        for _attr in self.attrs():
            _self_has, _other_has = hasattr(self, _attr), hasattr(other, _attr)
            if _self_has != _other_has:
                # One instance has `_attr` set; the other doesn't.
                return False
            if not _self_has:
                # Both instances don't have `_attr` set.
                continue
            if getattr(self, _attr) != getattr(other, _attr):
                return False
        return True

    @classmethod
    def attrs(cls) -> Dict[str, Type]:
        """Return a dictionary mapping attributes of the class to their types.

        Example::

            >>> class A(Corgy):
            ...     x: Annotated[int, "x"]
            ...     y: Sequence[str]

            >>> A.attrs()
            {'x': <class 'int'>, 'y': typing.Sequence[str]}

        """
        return {
            _attr: getattr(cls, _attr).fget.__annotations__["return"]
            for _attr in cls.__annotations__
        }

    def as_dict(self, recursive: bool = True, flatten: bool = False) -> Dict[str, Any]:
        """Return the object as a dictionary.

        The returned dictionary maps attribute names to their values. Unset attributes
        are omitted, unless they have default values.

        Args:
            recursive: whether to recursively call `as_dict` on attributes which are
                `Corgy` instances. Otherwise, they are returned as is.
            flatten: whether to flatten group arguments into `:` separated strings.
                Only takes effect if `recursive` is `True`.

        Examples::

            >>> class G(Corgy):
            ...     x: int

            >>> g = G(x=1)
            >>> g.as_dict()
            {'x': 1}

            >>> class A(Corgy):
            ...     x: str
            ...     g: G

            >>> a = A(x="one", g=g)
            >>> a.as_dict(recursive=False)
            {'x': 'one', 'g': G(x=1)}
            >>> a.as_dict()
            {'x': 'one', 'g': {'x': 1}}
            >>> a.as_dict(flatten=True)
            {'x': 'one', 'g:x': 1}

        """

        def dictify_corgys(_val):
            _coll_type = get_concrete_collection_type(type(_val))
            if _coll_type is not None:
                _cast_type = _coll_type if _coll_type is not AbstractSequence else list
                return _cast_type(  # pylint: disable=abstract-class-instantiated
                    [dictify_corgys(_val_part) for _val_part in _val]
                )
            if isinstance(_val.__class__, _CorgyMeta):
                return _val.as_dict(recursive=True, flatten=flatten)
            return _val

        self_dict = {}
        for attr_name, attr_type in self.attrs().items():
            try:
                attr_val = getattr(self, attr_name)
            except AttributeError:
                continue

            if recursive:
                attr_val = dictify_corgys(attr_val)
                if flatten and isinstance(attr_type, _CorgyMeta):
                    for _k, _v in attr_val.items():
                        _flat_key = f"{attr_name}:{_k}"
                        self_dict[_flat_key] = _v
                    continue

            self_dict[attr_name] = attr_val

        return self_dict

    @classmethod
    def from_dict(cls: Type[_T], d: Mapping[str, Any], try_cast: bool = False) -> _T:
        """Return a new instance of the class using a dictionary.

        This is roughly equivalent to `cls(**d)`, with the main exception being that
        groups can be specified as dictionaries themselves, and will be processed
        recursively.

        Args:
            d: Dictionary to create the instance from.
            try_cast: Whether to try and cast values which don't match attribute types.

        Example::

            >>> class G(Corgy):
            ...     x: int

            >>> class A(Corgy):
            ...     x: str
            ...     g: G

            >>> A.from_dict({"x": "one", "g": G(x=1)})
            A(x='one', g=G(x=1))
            >>> A.from_dict({"x": "one", "g": {"x": 1}})
            A(x='one', g=G(x=1))
            >>> A.from_dict({"x": "one", "g": {"x": "1"}}, try_cast=True)
            A(x='one', g=G(x=1))

        Group attributes can also be passed directly in the dictionary by prefixing
        their names with the group name and a colon::

            >>> A.from_dict({"x": "one", "g:x": 1})
            A(x='one', g=G(x=1))

            >>> class B(Corgy):
            ...     x: float
            ...     a: A

            >>> B.from_dict({"x": 1.1, "a:x": "one", "a:g:x": 1})
            B(x=1.1, a=A(x='one', g=G(x=1)))

        """
        main_args_map = {}
        grp_args_map: Dict[str, Any] = defaultdict(dict)
        cls_attrs = cls.attrs()

        for arg_name, arg_val in d.items():
            if ":" in arg_name:
                grp_name, arg_name_base = arg_name.split(":", maxsplit=1)
                if not hasattr(cls, grp_name):
                    raise ValueError(
                        f"invalid argument `{arg_name}`: "
                        f"`{cls}` has no group named `{grp_name}`"
                    )
                if grp_name in d:
                    raise ValueError(
                        f"conflicting arguments: `{arg_name}` and `{grp_name}`"
                    )
                grp_type = cls_attrs[grp_name]
                if not isinstance(grp_type, _CorgyMeta):
                    raise ValueError(f"`{grp_name}` is not a `Corgy` class")
                grp_args_map[grp_name][arg_name_base] = arg_val

            elif hasattr(cls, arg_name):
                arg_type = cls_attrs[arg_name]
                if isinstance(arg_type, _CorgyMeta) and isinstance(arg_val, dict):
                    grp_args_map[arg_name] = arg_val
                else:
                    main_args_map[arg_name] = arg_val

        for grp_name, grp_args in grp_args_map.items():
            grp_type = cls_attrs[grp_name]
            main_args_map[grp_name] = grp_type.from_dict(grp_args, try_cast)

        cls_attrs = cls.attrs()
        for arg_name, arg_val in main_args_map.copy().items():
            if arg_name in cls_attrs:
                main_args_map[arg_name] = check_val_type(
                    arg_val, cls_attrs[arg_name], try_cast, try_load_corgy_dicts=True
                )
        return cls(**main_args_map)

    def load_dict(
        self, d: Dict[str, Any], try_cast: bool = False, strict: bool = False
    ) -> None:
        """Load a dictionary into an instance of the class.

        Previous attributes are overwritten. Sub-dictionaries will be parsed
        recursively if the corresponding attribute already exists, else will be parsed
        using `from_dict`. As with `from_dict`, items in the dictionary without
        corresponding attributes are ignored.

        Args:
            d: Dictionary to load.
            try_cast: Whether to try and cast values which don't match attribute types.
            strict: If `True`, attributes with existing values that are not in the
                dictionary will be unset.

        Example::

            >>> class A(Corgy):
            ...     x: int
            ...     y: str
            >>> a = A(x=1)
            >>> _i = id(a)
            >>> a.load_dict({"y": "two"})
            >>> a
            A(x=1, y='two')
            >>> _i == id(a)
            True
            >>> a.load_dict({"y": "three"}, strict=True)
            >>> a
            A(y='three')
            >>> _i == id(a)
            True

        """
        main_args_map: Dict[str, Any] = defaultdict(dict)
        cls_attrs = self.attrs()

        for arg_name, arg_val in d.items():
            if ":" in arg_name:
                grp_name, arg_name_base = arg_name.split(":", maxsplit=1)
                if grp_name not in cls_attrs:
                    raise ValueError(
                        f"invalid argument `{arg_name}`: "
                        f"`{self.__class__}` has no group named `{grp_name}`"
                    )
                if grp_name in d:
                    raise ValueError(
                        f"conflicting arguments: `{arg_name}` and `{grp_name}`"
                    )
                grp_type = cls_attrs[grp_name]
                if not isinstance(grp_type, _CorgyMeta):
                    raise ValueError(f"`{grp_name}` is not a `Corgy` class")
                main_args_map[grp_name][arg_name_base] = arg_val

            elif arg_name in cls_attrs:
                main_args_map[arg_name] = arg_val

        for arg_name, arg_type in cls_attrs.items():
            if arg_name not in main_args_map:
                if strict:
                    try:
                        delattr(self, arg_name)
                    except AttributeError:
                        pass
                continue

            arg_new_val = main_args_map[arg_name]
            if isinstance(arg_type, _CorgyMeta) and isinstance(arg_new_val, dict):
                try:
                    arg_obj = getattr(self, arg_name)
                except AttributeError:
                    setattr(self, arg_name, arg_type.from_dict(arg_new_val))
                else:
                    arg_obj.load_dict(arg_new_val, try_cast, strict)
            else:
                arg_new_val = check_val_type(
                    arg_new_val, arg_type, try_cast, try_load_corgy_dicts=True
                )
                setattr(self, arg_name, arg_new_val)

    @classmethod
    def parse_from_cmdline(
        cls: Type[_T],
        parser: Optional[argparse.ArgumentParser] = None,
        defaults: Optional[Mapping[str, Any]] = None,
        **parser_args,
    ) -> _T:
        """Return an instance of the class parsed from command line arguments.

        Args:
            parser: An instance of `argparse.ArgumentParser` or `None`. If `None`, a new
                instance is created.
            defaults: A dictionary of default values for the attributes, passed to
                `add_args_to_parser`. Refer to the docs for `add_args_to_parser` for
                more details.
            parser_args: Arguments to be passed to `argparse.ArgumentParser()`. Ignored
                if `parser` is not None.

        Raises:
            ArgumentError: Error parsing command line arguments.
        """
        if parser is None:
            if "formatter_class" not in parser_args:
                parser_args["formatter_class"] = CorgyHelpFormatter
            parser = argparse.ArgumentParser(**parser_args)
        cls.add_args_to_parser(parser, defaults=defaults)
        args = vars(parser.parse_args())
        return cls.from_dict(args, try_cast=True)

    @classmethod
    def parse_from_toml(
        cls: Type[_T],
        toml_file: IO[bytes],
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> _T:
        """Parse an object of the class from a toml file.

        Args:
            toml_file: A file-like object containing the class attributes in toml.
            defaults: A dictionary of default values, overriding any values specified
                in the class.

        Raises:
            TOMLDecodeError: Error parsing the toml file.

        Example::

            >>> class G(Corgy):
            ...     x: int
            ...     y: Sequence[int]

            >>> class A(Corgy):
            ...     x: str
            ...     g: G

            >>> from io import BytesIO
            >>> f = BytesIO(b'''
            ...     x = 'one'
            ...     [g]
            ...     x = 1
            ...     y = [1, 2, 3]
            ... ''')

            >>> A.parse_from_toml(f)  # doctest: +SKIP
            A(x='one', g=G(x=1, y=[1, 2, 3]))

        """
        tomli = importlib.import_module(
            "tomllib" if sys.version_info >= (3, 11) else "tomli"
        )
        toml_data = tomli.load(toml_file)
        if defaults is not None:
            for _k, _v in defaults.items():
                if _k not in toml_data:
                    toml_data[_k] = _v
        _parsers = getattr(cls, "__parsers")
        for _k, _v in toml_data.items():
            if _k in _parsers:
                toml_data[_k] = _parsers[_k](_v)
        return cls.from_dict(toml_data, try_cast=True)


class _CorgyParser(NamedTuple):
    """Class to represent custom parsers.

    This class is returned by the `@corgyparser` decorator, and is used by `Corgy` to
    keep track of parsers.
    """

    var_names: List[str]
    fparse: Callable[[str], Any]
    nargs: Union[None, Literal["*", "+"], int]

    def __call__(self, s):
        return self.fparse(s)


def corgyparser(
    *var_names: str,
    metavar: Optional[str] = None,
    nargs: Union[None, Literal["*", "+"], int] = None,
) -> Callable[[Union[Callable[[str], Any], _CorgyParser]], _CorgyParser]:
    """Decorate a function as a custom parser for one or more attributes.

    To use a custom function for parsing a `Corgy` attribute, use this decorator.
    Parsing functions must be static, and should only accept a single argument.
    Decorating the function with `@staticmethod` is optional, but prevents type errors.
    `@corgyparser` must be the final decorator in the decorator chain.

    Args:
        var_names: The attributes associated with the decorated parser.
        metavar: Keyword only argument to set the metavar when adding the associated
            attribute(s) to an `ArgumentParser` instance.
        nargs: Keyword only argument to set the number of arguments to be used for the
            associated attribute(s). Must be `None`, `'*'`, `'+'`, or a positive number.
            This value is passed as the `nargs` argument to
            `ArgumentParser.add_argument`, and controls the number of arguments that
            will be read from the command line, and passed to the parsing function.
            For all values other than `None`, the parsing function will receive a list
            of strings.

    Example::

        >>> from corgy import corgyparser

        >>> class A(Corgy):
        ...     time: Tuple[int, int, int]
        ...     @corgyparser("time", metavar="int:int:int")
        ...     @staticmethod
        ...     def parse_time(s):
        ...         return tuple(map(int, s.split(":")))

        >>> parser = ArgumentParser(
        ...     formatter_class=CorgyHelpFormatter,
        ...     add_help=False,
        ...     usage=SUPPRESS,
        ... )

        >>> A.add_args_to_parser(parser)
        >>> parser.parse_args(["--time", "1:2:3"])
        Namespace(time=(1, 2, 3))

    Multiple arguments can be passed to the decorator, and will all be associated with
    the same parser::

        >>> class A(Corgy):
        ...     x: int
        ...     y: int
        ...     @corgyparser("x", "y")
        ...     @staticmethod
        ...     def parse_x_y(s):
        ...         return int(s)

    The `@corgyparser` decorator can also be chained to use the same parser for multiple
    arguments::

        >>> class A(Corgy):
        ...     x: int
        ...     y: int
        ...     @corgyparser("x")
        ...     @corgyparser("y")
        ...     @staticmethod
        ...     def parse_x_y(s):
        ...         return int(s)

    Note: when chaining, the outer-most non-`None` value of `metavar` will be used.

    Custom parsers can control the number of arguments they receive, independent of the
    argument type::

        >>> class A(Corgy):
        ...     x: int
        ...     @corgyparser("x", nargs=3)
        ...     @staticmethod
        ...     def parse_x(s):
        ...         # `s` will be a list of 3 strings.
        ...         return sum(map(int, s))

        >>> parser = ArgumentParser(
        ...     formatter_class=CorgyHelpFormatter,
        ...     add_help=False,
        ...     usage=SUPPRESS,
        ... )

        >>> A.add_args_to_parser(parser)
        >>> parser.parse_args(["--x", "1", "2", "3"])
        Namespace(x=6)

    When chaining, `nargs` must be the same for all decorators, otherwise `TypeError` is
    raised.
    """
    if not all(isinstance(_var_name, str) for _var_name in var_names):
        raise TypeError(
            "corgyparser should be passed the name of arguments: decorate using"
            "@corgyparser(<argument(s)>)"
        )

    def wrapper(var_names, metavar, fparse):
        if isinstance(fparse, _CorgyParser):
            if nargs != fparse.nargs:
                raise TypeError(
                    "all `corgyparser` decorations of a funciton must have same `nargs`"
                )
            corgy_parser = fparse
            corgy_parser.var_names.extend(var_names)
        else:
            if isinstance(fparse, staticmethod):
                fparse = fparse.__func__
            if not callable(fparse):
                raise TypeError("corgyparser can only decorate static functions")
            corgy_parser = _CorgyParser(list(var_names), fparse, nargs)

        if metavar is not None:
            setattr(corgy_parser.fparse, "__metavar__", metavar)
        if nargs is not None:
            setattr(corgy_parser.fparse, "__nargs__", nargs)
        return corgy_parser

    return partial(wrapper, var_names, metavar)


class CorgyParserAction(argparse.Action):
    def __init__(self, corgy_parser: _CorgyParser, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._corgy_parser = corgy_parser

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            setattr(namespace, self.dest, self._corgy_parser(values))
        except ValueError as e:
            raise argparse.ArgumentTypeError from e
