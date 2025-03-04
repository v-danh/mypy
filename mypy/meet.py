from mypy.backports import OrderedDict
from typing import List, Optional, Tuple, Callable

from mypy.types import (
    Type, AnyType, TypeVisitor, UnboundType, NoneType, TypeVarType, Instance, CallableType,
    TupleType, TypedDictType, ErasedType, UnionType, PartialType, DeletedType,
    UninhabitedType, TypeType, TypeOfAny, Overloaded, FunctionLike, LiteralType,
    ProperType, get_proper_type, get_proper_types, TypeAliasType, TypeGuardedType,
    ParamSpecType, Parameters, UnpackType,
)
from mypy.subtypes import is_equivalent, is_subtype, is_callable_compatible, is_proper_subtype
from mypy.erasetype import erase_type
from mypy.maptype import map_instance_to_supertype
from mypy.typeops import tuple_fallback, make_simplified_union, is_recursive_pair
from mypy.state import state
from mypy import join

# TODO Describe this module.


def trivial_meet(s: Type, t: Type) -> ProperType:
    """Return one of types (expanded) if it is a subtype of other, otherwise bottom type."""
    if is_subtype(s, t):
        return get_proper_type(s)
    elif is_subtype(t, s):
        return get_proper_type(t)
    else:
        if state.strict_optional:
            return UninhabitedType()
        else:
            return NoneType()


def meet_types(s: Type, t: Type) -> ProperType:
    """Return the greatest lower bound of two types."""
    if is_recursive_pair(s, t):
        # This case can trigger an infinite recursion, general support for this will be
        # tricky so we use a trivial meet (like for protocols).
        return trivial_meet(s, t)
    s = get_proper_type(s)
    t = get_proper_type(t)

    if isinstance(s, ErasedType):
        return s
    if isinstance(s, AnyType):
        return t
    if isinstance(s, UnionType) and not isinstance(t, UnionType):
        s, t = t, s
    return t.accept(TypeMeetVisitor(s))


def narrow_declared_type(declared: Type, narrowed: Type) -> Type:
    """Return the declared type narrowed down to another type."""
    # TODO: check infinite recursion for aliases here.
    if isinstance(narrowed, TypeGuardedType):  # type: ignore[misc]
        # A type guard forces the new type even if it doesn't overlap the old.
        return narrowed.type_guard

    declared = get_proper_type(declared)
    narrowed = get_proper_type(narrowed)

    if declared == narrowed:
        return declared
    if isinstance(declared, UnionType):
        return make_simplified_union([narrow_declared_type(x, narrowed)
                                      for x in declared.relevant_items()])
    elif not is_overlapping_types(declared, narrowed,
                                  prohibit_none_typevar_overlap=True):
        if state.strict_optional:
            return UninhabitedType()
        else:
            return NoneType()
    elif isinstance(narrowed, UnionType):
        return make_simplified_union([narrow_declared_type(declared, x)
                                      for x in narrowed.relevant_items()])
    elif isinstance(narrowed, AnyType):
        return narrowed
    elif isinstance(narrowed, TypeVarType) and is_subtype(narrowed.upper_bound, declared):
        return narrowed
    elif isinstance(declared, TypeType) and isinstance(narrowed, TypeType):
        return TypeType.make_normalized(narrow_declared_type(declared.item, narrowed.item))
    elif (isinstance(declared, TypeType)
          and isinstance(narrowed, Instance)
          and narrowed.type.is_metaclass()):
        # We'd need intersection types, so give up.
        return declared
    elif isinstance(declared, (Instance, TupleType, TypeType, LiteralType)):
        return meet_types(declared, narrowed)
    elif isinstance(declared, TypedDictType) and isinstance(narrowed, Instance):
        # Special case useful for selecting TypedDicts from unions using isinstance(x, dict).
        if (narrowed.type.fullname == 'builtins.dict' and
                all(isinstance(t, AnyType) for t in get_proper_types(narrowed.args))):
            return declared
        return meet_types(declared, narrowed)
    return narrowed


def get_possible_variants(typ: Type) -> List[Type]:
    """This function takes any "Union-like" type and returns a list of the available "options".

    Specifically, there are currently exactly three different types that can have
    "variants" or are "union-like":

    - Unions
    - TypeVars with value restrictions
    - Overloads

    This function will return a list of each "option" present in those types.

    If this function receives any other type, we return a list containing just that
    original type. (E.g. pretend the type was contained within a singleton union).

    The only exception is regular TypeVars: we return a list containing that TypeVar's
    upper bound.

    This function is useful primarily when checking to see if two types are overlapping:
    the algorithm to check if two unions are overlapping is fundamentally the same as
    the algorithm for checking if two overloads are overlapping.

    Normalizing both kinds of types in the same way lets us reuse the same algorithm
    for both.
    """
    typ = get_proper_type(typ)

    if isinstance(typ, TypeVarType):
        if len(typ.values) > 0:
            return typ.values
        else:
            return [typ.upper_bound]
    elif isinstance(typ, UnionType):
        return list(typ.items)
    elif isinstance(typ, Overloaded):
        # Note: doing 'return typ.items()' makes mypy
        # infer a too-specific return type of List[CallableType]
        return list(typ.items)
    else:
        return [typ]


def is_overlapping_types(left: Type,
                         right: Type,
                         ignore_promotions: bool = False,
                         prohibit_none_typevar_overlap: bool = False) -> bool:
    """Can a value of type 'left' also be of type 'right' or vice-versa?

    If 'ignore_promotions' is True, we ignore promotions while checking for overlaps.
    If 'prohibit_none_typevar_overlap' is True, we disallow None from overlapping with
    TypeVars (in both strict-optional and non-strict-optional mode).
    """
    if (
        isinstance(left, TypeGuardedType)  # type: ignore[misc]
        or isinstance(right, TypeGuardedType)  # type: ignore[misc]
    ):
        # A type guard forces the new type even if it doesn't overlap the old.
        return True

    left, right = get_proper_types((left, right))

    def _is_overlapping_types(left: Type, right: Type) -> bool:
        '''Encode the kind of overlapping check to perform.

        This function mostly exists so we don't have to repeat keyword arguments everywhere.'''
        return is_overlapping_types(
            left, right,
            ignore_promotions=ignore_promotions,
            prohibit_none_typevar_overlap=prohibit_none_typevar_overlap)

    # We should never encounter this type.
    if isinstance(left, PartialType) or isinstance(right, PartialType):
        assert False, "Unexpectedly encountered partial type"

    # We should also never encounter these types, but it's possible a few
    # have snuck through due to unrelated bugs. For now, we handle these
    # in the same way we handle 'Any'.
    #
    # TODO: Replace these with an 'assert False' once we are more confident.
    illegal_types = (UnboundType, ErasedType, DeletedType)
    if isinstance(left, illegal_types) or isinstance(right, illegal_types):
        return True

    # When running under non-strict optional mode, simplify away types of
    # the form 'Union[A, B, C, None]' into just 'Union[A, B, C]'.

    if not state.strict_optional:
        if isinstance(left, UnionType):
            left = UnionType.make_union(left.relevant_items())
        if isinstance(right, UnionType):
            right = UnionType.make_union(right.relevant_items())
        left, right = get_proper_types((left, right))

    # 'Any' may or may not be overlapping with the other type
    if isinstance(left, AnyType) or isinstance(right, AnyType):
        return True

    # We check for complete overlaps next as a general-purpose failsafe.
    # If this check fails, we start checking to see if there exists a
    # *partial* overlap between types.
    #
    # These checks will also handle the NoneType and UninhabitedType cases for us.

    if (is_proper_subtype(left, right, ignore_promotions=ignore_promotions)
            or is_proper_subtype(right, left, ignore_promotions=ignore_promotions)):
        return True

    # See the docstring for 'get_possible_variants' for more info on what the
    # following lines are doing.

    left_possible = get_possible_variants(left)
    right_possible = get_possible_variants(right)

    # We start by checking multi-variant types like Unions first. We also perform
    # the same logic if either type happens to be a TypeVar.
    #
    # Handling the TypeVars now lets us simulate having them bind to the corresponding
    # type -- if we deferred these checks, the "return-early" logic of the other
    # checks will prevent us from detecting certain overlaps.
    #
    # If both types are singleton variants (and are not TypeVars), we've hit the base case:
    # we skip these checks to avoid infinitely recursing.

    def is_none_typevar_overlap(t1: Type, t2: Type) -> bool:
        t1, t2 = get_proper_types((t1, t2))
        return isinstance(t1, NoneType) and isinstance(t2, TypeVarType)

    if prohibit_none_typevar_overlap:
        if is_none_typevar_overlap(left, right) or is_none_typevar_overlap(right, left):
            return False

    if (len(left_possible) > 1 or len(right_possible) > 1
            or isinstance(left, TypeVarType) or isinstance(right, TypeVarType)):
        for l in left_possible:
            for r in right_possible:
                if _is_overlapping_types(l, r):
                    return True
        return False

    # Now that we've finished handling TypeVars, we're free to end early
    # if one one of the types is None and we're running in strict-optional mode.
    # (None only overlaps with None in strict-optional mode).
    #
    # We must perform this check after the TypeVar checks because
    # a TypeVar could be bound to None, for example.

    if state.strict_optional and isinstance(left, NoneType) != isinstance(right, NoneType):
        return False

    # Next, we handle single-variant types that may be inherently partially overlapping:
    #
    # - TypedDicts
    # - Tuples
    #
    # If we cannot identify a partial overlap and end early, we degrade these two types
    # into their 'Instance' fallbacks.

    if isinstance(left, TypedDictType) and isinstance(right, TypedDictType):
        return are_typed_dicts_overlapping(left, right, ignore_promotions=ignore_promotions)
    elif typed_dict_mapping_pair(left, right):
        # Overlaps between TypedDicts and Mappings require dedicated logic.
        return typed_dict_mapping_overlap(left, right,
                                          overlapping=_is_overlapping_types)
    elif isinstance(left, TypedDictType):
        left = left.fallback
    elif isinstance(right, TypedDictType):
        right = right.fallback

    if is_tuple(left) and is_tuple(right):
        return are_tuples_overlapping(left, right, ignore_promotions=ignore_promotions)
    elif isinstance(left, TupleType):
        left = tuple_fallback(left)
    elif isinstance(right, TupleType):
        right = tuple_fallback(right)

    # Next, we handle single-variant types that cannot be inherently partially overlapping,
    # but do require custom logic to inspect.
    #
    # As before, we degrade into 'Instance' whenever possible.

    if isinstance(left, TypeType) and isinstance(right, TypeType):
        return _is_overlapping_types(left.item, right.item)

    def _type_object_overlap(left: Type, right: Type) -> bool:
        """Special cases for type object types overlaps."""
        # TODO: these checks are a bit in gray area, adjust if they cause problems.
        left, right = get_proper_types((left, right))
        # 1. Type[C] vs Callable[..., C], where the latter is class object.
        if isinstance(left, TypeType) and isinstance(right, CallableType) and right.is_type_obj():
            return _is_overlapping_types(left.item, right.ret_type)
        # 2. Type[C] vs Meta, where Meta is a metaclass for C.
        if isinstance(left, TypeType) and isinstance(right, Instance):
            if isinstance(left.item, Instance):
                left_meta = left.item.type.metaclass_type
                if left_meta is not None:
                    return _is_overlapping_types(left_meta, right)
                # builtins.type (default metaclass) overlaps with all metaclasses
                return right.type.has_base('builtins.type')
            elif isinstance(left.item, AnyType):
                return right.type.has_base('builtins.type')
        # 3. Callable[..., C] vs Meta is considered below, when we switch to fallbacks.
        return False

    if isinstance(left, TypeType) or isinstance(right, TypeType):
        return _type_object_overlap(left, right) or _type_object_overlap(right, left)

    if isinstance(left, CallableType) and isinstance(right, CallableType):
        return is_callable_compatible(left, right,
                                      is_compat=_is_overlapping_types,
                                      ignore_pos_arg_names=True,
                                      allow_partial_overlap=True)
    elif isinstance(left, CallableType):
        left = left.fallback
    elif isinstance(right, CallableType):
        right = right.fallback

    if isinstance(left, LiteralType) and isinstance(right, LiteralType):
        if left.value == right.value:
            # If values are the same, we still need to check if fallbacks are overlapping,
            # this is done below.
            left = left.fallback
            right = right.fallback
        else:
            return False
    elif isinstance(left, LiteralType):
        left = left.fallback
    elif isinstance(right, LiteralType):
        right = right.fallback

    # Finally, we handle the case where left and right are instances.

    if isinstance(left, Instance) and isinstance(right, Instance):
        # First we need to handle promotions and structural compatibility for instances
        # that came as fallbacks, so simply call is_subtype() to avoid code duplication.
        if (is_subtype(left, right, ignore_promotions=ignore_promotions)
                or is_subtype(right, left, ignore_promotions=ignore_promotions)):
            return True

        # Two unrelated types cannot be partially overlapping: they're disjoint.
        if left.type.has_base(right.type.fullname):
            left = map_instance_to_supertype(left, right.type)
        elif right.type.has_base(left.type.fullname):
            right = map_instance_to_supertype(right, left.type)
        else:
            return False

        if len(left.args) == len(right.args):
            # Note: we don't really care about variance here, since the overlapping check
            # is symmetric and since we want to return 'True' even for partial overlaps.
            #
            # For example, suppose we have two types Wrapper[Parent] and Wrapper[Child].
            # It doesn't matter whether Wrapper is covariant or contravariant since
            # either way, one of the two types will overlap with the other.
            #
            # Similarly, if Wrapper was invariant, the two types could still be partially
            # overlapping -- what if Wrapper[Parent] happened to contain only instances of
            # specifically Child?
            #
            # Or, to use a more concrete example, List[Union[A, B]] and List[Union[B, C]]
            # would be considered partially overlapping since it's possible for both lists
            # to contain only instances of B at runtime.
            if all(_is_overlapping_types(left_arg, right_arg)
                   for left_arg, right_arg in zip(left.args, right.args)):
                return True

        return False

    # We ought to have handled every case by now: we conclude the
    # two types are not overlapping, either completely or partially.
    #
    # Note: it's unclear however, whether returning False is the right thing
    # to do when inferring reachability -- see  https://github.com/python/mypy/issues/5529

    assert type(left) != type(right)
    return False


def is_overlapping_erased_types(left: Type, right: Type, *,
                                ignore_promotions: bool = False) -> bool:
    """The same as 'is_overlapping_erased_types', except the types are erased first."""
    return is_overlapping_types(erase_type(left), erase_type(right),
                                ignore_promotions=ignore_promotions,
                                prohibit_none_typevar_overlap=True)


def are_typed_dicts_overlapping(left: TypedDictType, right: TypedDictType, *,
                                ignore_promotions: bool = False,
                                prohibit_none_typevar_overlap: bool = False) -> bool:
    """Returns 'true' if left and right are overlapping TypeDictTypes."""
    # All required keys in left are present and overlapping with something in right
    for key in left.required_keys:
        if key not in right.items:
            return False
        if not is_overlapping_types(left.items[key], right.items[key],
                                    ignore_promotions=ignore_promotions,
                                    prohibit_none_typevar_overlap=prohibit_none_typevar_overlap):
            return False

    # Repeat check in the other direction
    for key in right.required_keys:
        if key not in left.items:
            return False
        if not is_overlapping_types(left.items[key], right.items[key],
                                    ignore_promotions=ignore_promotions):
            return False

    # The presence of any additional optional keys does not affect whether the two
    # TypedDicts are partially overlapping: the dicts would be overlapping if the
    # keys happened to be missing.
    return True


def are_tuples_overlapping(left: Type, right: Type, *,
                           ignore_promotions: bool = False,
                           prohibit_none_typevar_overlap: bool = False) -> bool:
    """Returns true if left and right are overlapping tuples."""
    left, right = get_proper_types((left, right))
    left = adjust_tuple(left, right) or left
    right = adjust_tuple(right, left) or right
    assert isinstance(left, TupleType), 'Type {} is not a tuple'.format(left)
    assert isinstance(right, TupleType), 'Type {} is not a tuple'.format(right)
    if len(left.items) != len(right.items):
        return False
    return all(is_overlapping_types(l, r,
                                    ignore_promotions=ignore_promotions,
                                    prohibit_none_typevar_overlap=prohibit_none_typevar_overlap)
               for l, r in zip(left.items, right.items))


def adjust_tuple(left: ProperType, r: ProperType) -> Optional[TupleType]:
    """Find out if `left` is a Tuple[A, ...], and adjust its length to `right`"""
    if isinstance(left, Instance) and left.type.fullname == 'builtins.tuple':
        n = r.length() if isinstance(r, TupleType) else 1
        return TupleType([left.args[0]] * n, left)
    return None


def is_tuple(typ: Type) -> bool:
    typ = get_proper_type(typ)
    return (isinstance(typ, TupleType)
            or (isinstance(typ, Instance) and typ.type.fullname == 'builtins.tuple'))


class TypeMeetVisitor(TypeVisitor[ProperType]):
    def __init__(self, s: ProperType) -> None:
        self.s = s

    def visit_unbound_type(self, t: UnboundType) -> ProperType:
        if isinstance(self.s, NoneType):
            if state.strict_optional:
                return AnyType(TypeOfAny.special_form)
            else:
                return self.s
        elif isinstance(self.s, UninhabitedType):
            return self.s
        else:
            return AnyType(TypeOfAny.special_form)

    def visit_any(self, t: AnyType) -> ProperType:
        return self.s

    def visit_union_type(self, t: UnionType) -> ProperType:
        if isinstance(self.s, UnionType):
            meets: List[Type] = []
            for x in t.items:
                for y in self.s.items:
                    meets.append(meet_types(x, y))
        else:
            meets = [meet_types(x, self.s)
                     for x in t.items]
        return make_simplified_union(meets)

    def visit_none_type(self, t: NoneType) -> ProperType:
        if state.strict_optional:
            if isinstance(self.s, NoneType) or (isinstance(self.s, Instance) and
                                               self.s.type.fullname == 'builtins.object'):
                return t
            else:
                return UninhabitedType()
        else:
            return t

    def visit_uninhabited_type(self, t: UninhabitedType) -> ProperType:
        return t

    def visit_deleted_type(self, t: DeletedType) -> ProperType:
        if isinstance(self.s, NoneType):
            if state.strict_optional:
                return t
            else:
                return self.s
        elif isinstance(self.s, UninhabitedType):
            return self.s
        else:
            return t

    def visit_erased_type(self, t: ErasedType) -> ProperType:
        return self.s

    def visit_type_var(self, t: TypeVarType) -> ProperType:
        if isinstance(self.s, TypeVarType) and self.s.id == t.id:
            return self.s
        else:
            return self.default(self.s)

    def visit_param_spec(self, t: ParamSpecType) -> ProperType:
        if self.s == t:
            return self.s
        else:
            return self.default(self.s)

    def visit_unpack_type(self, t: UnpackType) -> ProperType:
        raise NotImplementedError

    def visit_parameters(self, t: Parameters) -> ProperType:
        # TODO: is this the right variance?
        if isinstance(self.s, Parameters) or isinstance(self.s, CallableType):
            if len(t.arg_types) != len(self.s.arg_types):
                return self.default(self.s)
            return t.copy_modified(
                arg_types=[meet_types(s_a, t_a) for s_a, t_a in zip(self.s.arg_types, t.arg_types)]
            )
        else:
            return self.default(self.s)

    def visit_instance(self, t: Instance) -> ProperType:
        if isinstance(self.s, Instance):
            if t.type == self.s.type:
                if is_subtype(t, self.s) or is_subtype(self.s, t):
                    # Combine type arguments. We could have used join below
                    # equivalently.
                    args: List[Type] = []
                    # N.B: We use zip instead of indexing because the lengths might have
                    # mismatches during daemon reprocessing.
                    for ta, sia in zip(t.args, self.s.args):
                        args.append(self.meet(ta, sia))
                    return Instance(t.type, args)
                else:
                    if state.strict_optional:
                        return UninhabitedType()
                    else:
                        return NoneType()
            else:
                if is_subtype(t, self.s):
                    return t
                elif is_subtype(self.s, t):
                    # See also above comment.
                    return self.s
                else:
                    if state.strict_optional:
                        return UninhabitedType()
                    else:
                        return NoneType()
        elif isinstance(self.s, FunctionLike) and t.type.is_protocol:
            call = join.unpack_callback_protocol(t)
            if call:
                return meet_types(call, self.s)
        elif isinstance(self.s, FunctionLike) and self.s.is_type_obj() and t.type.is_metaclass():
            if is_subtype(self.s.fallback, t):
                return self.s
            return self.default(self.s)
        elif isinstance(self.s, TypeType):
            return meet_types(t, self.s)
        elif isinstance(self.s, TupleType):
            return meet_types(t, self.s)
        elif isinstance(self.s, LiteralType):
            return meet_types(t, self.s)
        elif isinstance(self.s, TypedDictType):
            return meet_types(t, self.s)
        return self.default(self.s)

    def visit_callable_type(self, t: CallableType) -> ProperType:
        if isinstance(self.s, CallableType) and join.is_similar_callables(t, self.s):
            if is_equivalent(t, self.s):
                return join.combine_similar_callables(t, self.s)
            result = meet_similar_callables(t, self.s)
            # We set the from_type_type flag to suppress error when a collection of
            # concrete class objects gets inferred as their common abstract superclass.
            if not ((t.is_type_obj() and t.type_object().is_abstract) or
                    (self.s.is_type_obj() and self.s.type_object().is_abstract)):
                result.from_type_type = True
            if isinstance(get_proper_type(result.ret_type), UninhabitedType):
                # Return a plain None or <uninhabited> instead of a weird function.
                return self.default(self.s)
            return result
        elif isinstance(self.s, TypeType) and t.is_type_obj() and not t.is_generic():
            # In this case we are able to potentially produce a better meet.
            res = meet_types(self.s.item, t.ret_type)
            if not isinstance(res, (NoneType, UninhabitedType)):
                return TypeType.make_normalized(res)
            return self.default(self.s)
        elif isinstance(self.s, Instance) and self.s.type.is_protocol:
            call = join.unpack_callback_protocol(self.s)
            if call:
                return meet_types(t, call)
        return self.default(self.s)

    def visit_overloaded(self, t: Overloaded) -> ProperType:
        # TODO: Implement a better algorithm that covers at least the same cases
        # as TypeJoinVisitor.visit_overloaded().
        s = self.s
        if isinstance(s, FunctionLike):
            if s.items == t.items:
                return Overloaded(t.items)
            elif is_subtype(s, t):
                return s
            elif is_subtype(t, s):
                return t
            else:
                return meet_types(t.fallback, s.fallback)
        elif isinstance(self.s, Instance) and self.s.type.is_protocol:
            call = join.unpack_callback_protocol(self.s)
            if call:
                return meet_types(t, call)
        return meet_types(t.fallback, s)

    def visit_tuple_type(self, t: TupleType) -> ProperType:
        if isinstance(self.s, TupleType) and self.s.length() == t.length():
            items: List[Type] = []
            for i in range(t.length()):
                items.append(self.meet(t.items[i], self.s.items[i]))
            # TODO: What if the fallbacks are different?
            return TupleType(items, tuple_fallback(t))
        elif isinstance(self.s, Instance):
            # meet(Tuple[t1, t2, <...>], Tuple[s, ...]) == Tuple[meet(t1, s), meet(t2, s), <...>].
            if self.s.type.fullname == 'builtins.tuple' and self.s.args:
                return t.copy_modified(items=[meet_types(it, self.s.args[0]) for it in t.items])
            elif is_proper_subtype(t, self.s):
                # A named tuple that inherits from a normal class
                return t
        return self.default(self.s)

    def visit_typeddict_type(self, t: TypedDictType) -> ProperType:
        if isinstance(self.s, TypedDictType):
            for (name, l, r) in self.s.zip(t):
                if (not is_equivalent(l, r) or
                        (name in t.required_keys) != (name in self.s.required_keys)):
                    return self.default(self.s)
            item_list: List[Tuple[str, Type]] = []
            for (item_name, s_item_type, t_item_type) in self.s.zipall(t):
                if s_item_type is not None:
                    item_list.append((item_name, s_item_type))
                else:
                    # at least one of s_item_type and t_item_type is not None
                    assert t_item_type is not None
                    item_list.append((item_name, t_item_type))
            items = OrderedDict(item_list)
            fallback = self.s.create_anonymous_fallback()
            required_keys = t.required_keys | self.s.required_keys
            return TypedDictType(items, required_keys, fallback)
        elif isinstance(self.s, Instance) and is_subtype(t, self.s):
            return t
        else:
            return self.default(self.s)

    def visit_literal_type(self, t: LiteralType) -> ProperType:
        if isinstance(self.s, LiteralType) and self.s == t:
            return t
        elif isinstance(self.s, Instance) and is_subtype(t.fallback, self.s):
            return t
        else:
            return self.default(self.s)

    def visit_partial_type(self, t: PartialType) -> ProperType:
        # We can't determine the meet of partial types. We should never get here.
        assert False, 'Internal error'

    def visit_type_type(self, t: TypeType) -> ProperType:
        if isinstance(self.s, TypeType):
            typ = self.meet(t.item, self.s.item)
            if not isinstance(typ, NoneType):
                typ = TypeType.make_normalized(typ, line=t.line)
            return typ
        elif isinstance(self.s, Instance) and self.s.type.fullname == 'builtins.type':
            return t
        elif isinstance(self.s, CallableType):
            return self.meet(t, self.s)
        else:
            return self.default(self.s)

    def visit_type_alias_type(self, t: TypeAliasType) -> ProperType:
        assert False, "This should be never called, got {}".format(t)

    def meet(self, s: Type, t: Type) -> ProperType:
        return meet_types(s, t)

    def default(self, typ: Type) -> ProperType:
        if isinstance(typ, UnboundType):
            return AnyType(TypeOfAny.special_form)
        else:
            if state.strict_optional:
                return UninhabitedType()
            else:
                return NoneType()


def meet_similar_callables(t: CallableType, s: CallableType) -> CallableType:
    from mypy.join import join_types

    arg_types: List[Type] = []
    for i in range(len(t.arg_types)):
        arg_types.append(join_types(t.arg_types[i], s.arg_types[i]))
    # TODO in combine_similar_callables also applies here (names and kinds)
    # The fallback type can be either 'function' or 'type'. The result should have 'function' as
    # fallback only if both operands have it as 'function'.
    if t.fallback.type.fullname != 'builtins.function':
        fallback = t.fallback
    else:
        fallback = s.fallback
    return t.copy_modified(arg_types=arg_types,
                           ret_type=meet_types(t.ret_type, s.ret_type),
                           fallback=fallback,
                           name=None)


def meet_type_list(types: List[Type]) -> Type:
    if not types:
        # This should probably be builtins.object but that is hard to get and
        # it doesn't matter for any current users.
        return AnyType(TypeOfAny.implementation_artifact)
    met = types[0]
    for t in types[1:]:
        met = meet_types(met, t)
    return met


def typed_dict_mapping_pair(left: Type, right: Type) -> bool:
    """Is this a pair where one type is a TypedDict and another one is an instance of Mapping?

    This case requires a precise/principled consideration because there are two use cases
    that push the boundary the opposite ways: we need to avoid spurious overlaps to avoid
    false positives for overloads, but we also need to avoid spuriously non-overlapping types
    to avoid false positives with --strict-equality.
    """
    left, right = get_proper_types((left, right))
    assert not isinstance(left, TypedDictType) or not isinstance(right, TypedDictType)

    if isinstance(left, TypedDictType):
        _, other = left, right
    elif isinstance(right, TypedDictType):
        _, other = right, left
    else:
        return False
    return isinstance(other, Instance) and other.type.has_base('typing.Mapping')


def typed_dict_mapping_overlap(left: Type, right: Type,
                               overlapping: Callable[[Type, Type], bool]) -> bool:
    """Check if a TypedDict type is overlapping with a Mapping.

    The basic logic here consists of two rules:

    * A TypedDict with some required keys is overlapping with Mapping[str, <some type>]
      if and only if every key type is overlapping with <some type>. For example:

      - TypedDict(x=int, y=str) overlaps with Dict[str, Union[str, int]]
      - TypedDict(x=int, y=str) doesn't overlap with Dict[str, int]

      Note that any additional non-required keys can't change the above result.

    * A TypedDict with no required keys overlaps with Mapping[str, <some type>] if and
      only if at least one of key types overlaps with <some type>. For example:

      - TypedDict(x=str, y=str, total=False) overlaps with Dict[str, str]
      - TypedDict(x=str, y=str, total=False) doesn't overlap with Dict[str, int]
      - TypedDict(x=int, y=str, total=False) overlaps with Dict[str, str]

    As usual empty, dictionaries lie in a gray area. In general, List[str] and List[str]
    are considered non-overlapping despite empty list belongs to both. However, List[int]
    and List[<nothing>] are considered overlapping.

    So here we follow the same logic: a TypedDict with no required keys is considered
    non-overlapping with Mapping[str, <some type>], but is considered overlapping with
    Mapping[<nothing>, <nothing>]. This way we avoid false positives for overloads, and also
    avoid false positives for comparisons like SomeTypedDict == {} under --strict-equality.
    """
    left, right = get_proper_types((left, right))
    assert not isinstance(left, TypedDictType) or not isinstance(right, TypedDictType)

    if isinstance(left, TypedDictType):
        assert isinstance(right, Instance)
        typed, other = left, right
    else:
        assert isinstance(left, Instance)
        assert isinstance(right, TypedDictType)
        typed, other = right, left

    mapping = next(base for base in other.type.mro if base.fullname == 'typing.Mapping')
    other = map_instance_to_supertype(other, mapping)
    key_type, value_type = get_proper_types(other.args)

    # TODO: is there a cleaner way to get str_type here?
    fallback = typed.as_anonymous().fallback
    str_type = fallback.type.bases[0].args[0]  # typing._TypedDict inherits Mapping[str, object]

    # Special case: a TypedDict with no required keys overlaps with an empty dict.
    if isinstance(key_type, UninhabitedType) and isinstance(value_type, UninhabitedType):
        return not typed.required_keys

    if typed.required_keys:
        if not overlapping(key_type, str_type):
            return False
        return all(overlapping(typed.items[k], value_type) for k in typed.required_keys)
    else:
        if not overlapping(key_type, str_type):
            return False
        non_required = set(typed.items.keys()) - typed.required_keys
        return any(overlapping(typed.items[k], value_type) for k in non_required)
