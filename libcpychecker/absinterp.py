#   Copyright 2011 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2011 Red Hat, Inc.
#
#   This is free software: you can redistribute it and/or modify it
#   under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see
#   <http://www.gnu.org/licenses/>.

import gcc
import gccutils
import sys
from six import StringIO
from gccutils import get_src_for_loc, get_nonnull_arguments, check_isinstance
from collections import OrderedDict
from libcpychecker.utils import log, logging_enabled
from libcpychecker.types import *

class AbstractValue:
    def __init__(self, gcctype, loc):
        if gcctype:
            check_isinstance(gcctype, gcc.Type)
        if loc:
            check_isinstance(loc, gcc.Location)
        self.gcctype = gcctype
        self.loc = loc

    def __str__(self):
        if self.gcctype:
            result = '%s' % self.gcctype
        else:
            result = 'unknown type'
        if self.loc:
            result += ' from %s' % self.loc
        return result

    def __repr__(self):
        return ('%s(gcctype=%r, loc=%r)'
                % (self.__class__.__name__, str(self.gcctype), self.loc))

    def get_transitions_for_function_call(self, state, stmt):
        """
        For use for handling function pointers.  Return a list of Transition
        instances giving the outcome of calling this function ptr value
        """
        check_isinstance(state, State)
        check_isinstance(stmt, gcc.GimpleCall)
        returntype = stmt.fn.type.dereference.type
        return [state.mktrans_assignment(stmt.lhs,
                                      UnknownValue(returntype, stmt.loc),
                                      'calling %s' % self)]

class UnknownValue(AbstractValue):
    """
    A value that we know nothing about
    """
    def __str__(self):
        if self.gcctype:
            return 'unknown %s from %s' % (self.gcctype, self.loc)
        else:
            if self.loc:
                return 'unknown value from %s' % self.loc
            else:
                return 'unknown value'

    def __repr__(self):
        return 'UnknownValue(gcctype=%r, loc=%r)' % (self.gcctype, self.loc)

class ConcreteValue(AbstractValue):
    """
    A known, specific value (e.g. 0)
    """
    def __init__(self, gcctype, loc, value):
        check_isinstance(gcctype, gcc.Type)
        if loc:
            check_isinstance(loc, gcc.Location)
        self.gcctype = gcctype
        self.loc = loc
        self.value = value

    def __ne__(self, other):
        if isinstance(other, ConcreteValue):
            return self.value != other.value
        return NotImplemented

    def __str__(self):
        if self.loc:
            return '(%s)%r from %s' % (self.gcctype, self.value, self.loc)
        else:
            return '(%s)%r' % (self.gcctype, self.value)

    def __repr__(self):
        return 'ConcreteValue(gcctype=%r, loc=%r, value=%r)' % (str(self.gcctype), self.loc, self.value)

    def is_null_ptr(self):
        if isinstance(self.gcctype, gcc.PointerType):
            return self.value == 0


class PointerToRegion(AbstractValue):
    """A non-NULL pointer value, pointing at a specific Region"""
    def __init__(self, gcctype, loc, region):
        AbstractValue.__init__(self, gcctype, loc)
        check_isinstance(region, Region)
        self.region = region

    def __str__(self):
        if self.loc:
            return '(%s)&%r from %s' % (self.gcctype, self.region, self.loc)
        else:
            return '(%s)&%r' % (self.gcctype, self.region)

    def __repr__(self):
        return 'PointerToRegion(gcctype=%r, loc=%r, region=%r)' % (str(self.gcctype), self.loc, self.region)

class DeallocatedMemory(AbstractValue):
    def __str__(self):
        if self.loc:
            return 'memory deallocated at %s' % self.loc
        else:
            return 'deallocated memory'

class UninitializedData(AbstractValue):
    def __str__(self):
        if self.loc:
            return 'uninitialized data at %s' % self.loc
        else:
            return 'uninitialized data'

class PredictedError(Exception):
    pass

class InvalidlyNullParameter(PredictedError):
    # Use this when we can predict that a function is called with NULL as an
    # argument for an argument that must not be NULL
    def __init__(self, fnname, paramidx, nullvalue):
        self.fnname = fnname
        self.paramidx = paramidx # starts at 1
        self.nullvalue = nullvalue

    def __str__(self):
        return ('%s can be called with NULL as parameter %i; %s'
                % (self.fnname, self.paramidx, self.nullvalue))


class NullPtrDereference(PredictedError):
    def __init__(self, state, expr, ptr, isdefinite):
        check_isinstance(state, State)
        check_isinstance(expr, (gcc.ComponentRef, gcc.MemRef))
        check_isinstance(ptr, AbstractValue)
        self.state = state
        self.expr = expr
        self.ptr = ptr
        self.isdefinite = isdefinite

    def __str__(self):
        if self.isdefinite:
            return ('dereferencing NULL (%s) at %s'
                    % (self.expr, self.state.loc.get_stmt().loc))
        else:
            return ('possibly dereferencing NULL (%s) at %s'
                    % (self.expr, self.state.loc.get_stmt().loc))

class ReadFromDeallocatedMemory(PredictedError):
    def __init__(self, stmt, value):
        check_isinstance(stmt, gcc.Gimple)
        check_isinstance(value, DeallocatedMemory)
        self.stmt = stmt
        self.value = value

    def __str__(self):
        return ('reading from deallocated memory at %s: %s'
                % (self.stmt.loc, self.value))

def describe_stmt(stmt):
    if isinstance(stmt, gcc.GimpleCall):
        if isinstance(stmt.fn.operand, gcc.FunctionDecl):
            fnname = stmt.fn.operand.name
            return 'call to %s at line %i' % (fnname, stmt.loc.line)
    else:
        return str(stmt.loc)

class Location:
    """A location within a CFG: a gcc.BasicBlock together with an index into
    the gimple list.  (We don't support SSA passes)"""
    def __init__(self, bb, idx):
        check_isinstance(bb, gcc.BasicBlock)
        check_isinstance(idx, int)
        self.bb = bb
        self.idx = idx

    def __str__(self):
        stmt = self.get_stmt()
        return ('block %i stmt:%i : %20r : %s'
                % (self.bb.index, self.idx, stmt, stmt))

    def next_locs(self):
        """Get a list of Location instances, for what can happen next"""
        if self.bb.gimple and len(self.bb.gimple) > self.idx + 1:
            # Next gimple statement:
            return [Location(self.bb, self.idx + 1)]
        else:
            # At end of gimple statements: successor BBs:
            return [Location.get_block_start(outedge.dest) for outedge in self.bb.succs]

    def next_loc(self):
        """Get the next Location, for when it's unique"""
        if self.bb.gimple:
            if len(self.bb.gimple) > self.idx + 1:
                # Next gimple statement:
                return Location(self.bb, self.idx + 1)
            else:
                assert len(self.bb.succs) == 1
                return Location.get_block_start(self.bb.succs[0].dest)

    def __eq__(self, other):
        return self.bb == other.bb and self.idx == other.idx

    @classmethod
    def get_block_start(cls, bb):
        # Don't bother iterating through phi_nodes if there aren't any:
        return Location(bb, 0)

    def get_stmt(self):
        if self.bb.gimple:
            return self.bb.gimple[self.idx]
        else:
            return None

    def get_gcc_loc(self):
        stmt = self.get_stmt()
        if stmt:
            return stmt.loc
        else:
            return None

class Region:
    def __init__(self, name, parent):
        self.name = name
        self.parent = parent
        self.children = []
        self.fields = {}
        if parent:
            parent.children.append(self)

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.name)

    def is_on_stack(self):
        if isinstance(self, RegionOnStack):
            return True
        if self.parent:
            return self.parent.is_on_stack()
        return False

class RegionForGlobal(Region):
    """
    Represents the area of memory (e.g. in .data or .bss section)
    used to store a particular globa
    """
    def __init__(self, vardecl):
        check_isinstance(vardecl, gcc.VarDecl)
        Region.__init__(self, vardecl.name, None)
        self.vardecl = vardecl

    def __repr__(self):
        return 'RegionForGlobal(%r)' % self.vardecl

class RegionOnStack(Region):
    def __repr__(self):
        return 'RegionOnStack(%r)' % self.name

    def __str__(self):
        return '%s on stack' % self.name

class RegionOnHeap(Region):
    """
    Represents an area of memory allocated on the heap
    """
    def __init__(self, name, alloc_stmt):
        check_isinstance(alloc_stmt, gcc.Gimple)
        Region.__init__(self, name, None)
        self.alloc_stmt = alloc_stmt

    def __repr__(self):
        return 'RegionOnHeap(%r, %r)' % (self.name, self.alloc_stmt.loc)

    def __str__(self):
        return '%s allocated at %s' % (self.name, self.alloc_stmt.loc)


class RegionForStringConstant(Region):
    """
    Represents an area of memory used for string constants
    typically allocated in the .data segment
    """
    def __init__(self, text):
        Region.__init__(self, text, None)
        self.text = text

class MissingValue(Exception):
    """
    The value tracking system couldn't figure out any information about the
    given region
    """
    def __init__(self, region):
        self.region = region

    def __str__(self):
        return 'Missing value for %s' % self.region

class SplitValue(Exception):
    """
    We encountered an value (e.g. UnknownValue), but we'd like to know more
    about it.

    Backtrack the analysis, splitting it into multiple possible worlds
    with alternate abstract values for said value
    """
    def __init__(self, value, altvalues):
        self.value = value
        self.altvalues = altvalues

    def __str__(self):
        return ('Splitting:\n%r\ninto\n%s'
                % (self.value,
                   '\n'.join([repr(alt) for alt in self.altvalues])))

    def split(self, state):
        log('creating states for split of %s into %s', self.value, self.altvalues)
        result = []
        for altvalue in self.altvalues:
            log(' creating state for split where %s is %s', self.value, altvalue)
            altvalue.fromsplit = True

            newstate = state.copy()
            newstate.fromsplit = True
            for r in newstate.value_for_region:
                # Replace instances of the value itself:
                if newstate.value_for_region[r] is self.value:
                    log('  replacing value for region %s with %s', r, altvalue)
                    newstate.value_for_region[r] = altvalue
            result.append(Transition(state,
                                     newstate,
                                     "treating %s as %s" % (self.value, altvalue)))
        return result

class State:
    """A Location with memory state"""
    def __init__(self, loc, region_for_var=None, value_for_region=None,
                 return_rvalue=None, has_returned=False, not_returning=False):
        check_isinstance(loc, Location)
        self.loc = loc

        # Mapping from VarDecl.name to Region:
        if region_for_var:
            check_isinstance(region_for_var, OrderedDict)
            self.region_for_var = region_for_var
        else:
            self.region_for_var = OrderedDict()

        # Mapping from Region to AbstractValue:
        if value_for_region:
            check_isinstance(value_for_region, OrderedDict)
            self.value_for_region = value_for_region
        else:
            self.value_for_region = OrderedDict()

        self.return_rvalue = return_rvalue
        self.has_returned = has_returned
        self.not_returning = not_returning

    def __str__(self):
        return ('loc: %s region_for_var:%s value_for_region:%s%s'
                % (self.loc,
                   self.region_for_var,
                   self.value_for_region,
                   self._extra()))

    def __repr__(self):
        return ('loc: %r region_for_var:%r value_for_region:%r%r'
                % (self.loc,
                   self.region_for_var,
                   self.value_for_region,
                   self._extra()))

    def log(self, logger):
        if not logging_enabled:
            return
        # Display data in tabular form:
        from gccutils import Table
        t = Table(['Expression', 'Region', 'Value'])
        for k in self.region_for_var:
            region = self.region_for_var[k]
            value = self.value_for_region.get(region, None)
            t.add_row((k, region, value),)
        s = StringIO()
        t.write(s)
        logger('%s', s.getvalue())

        #logger('extra: %s' % (self._extra(), ), indent)

        # FIXME: derived class/extra:
        #self.resources.log(logger, indent)

        logger('loc: %s', self.loc)
        if self.loc.get_stmt():
            logger('%s', self.loc.get_stmt().loc)

    def copy(self):
        c = self.__class__(loc,
                           self.region_for_var.copy(),
                           self.value_for_region.copy(),
                           self.return_rvalue,
                           self.has_returned,
                           self.not_returning)
        if hasattr(self, 'fun'):
            c.fun = self.fun
        return c

    def verify(self):
        """
        Perform self-tests to ensure sanity of this State
        """
        for k in self.value_for_region:
            check_isinstance(k, Region)
            if not isinstance(self.value_for_region[k], AbstractValue):
                raise TypeError('value for region %r is not an AbstractValue: %r'
                                % (k, self.value_for_region[k]))

    def eval_lvalue(self, expr, loc):
        """
        Return the Region for the given expression
        """
        log('eval_lvalue: %r %s', expr, expr)
        if loc:
            check_isinstance(loc, gcc.Location)
        if isinstance(expr, (gcc.VarDecl, gcc.ParmDecl)):
            region = self.var_region(expr)
            check_isinstance(region, Region)
            return region
        elif isinstance(expr, gcc.ArrayRef):
            region = self.element_region(expr, loc)
            check_isinstance(region, Region)
            return region
        elif isinstance(expr, gcc.ComponentRef):
            check_isinstance(expr.field, gcc.FieldDecl)
            return self.get_field_region(expr, loc)
        elif isinstance(expr, gcc.StringCst):
            region = self.string_constant_region(expr, loc)
            check_isinstance(region, Region)
            return region
        elif isinstance(expr, gcc.MemRef):
            # Write through a pointer:
            dest_ptr = self.eval_rvalue(expr.operand, loc)
            log('dest_ptr: %r', dest_ptr)
            self.raise_any_null_ptr_deref(expr, dest_ptr)
            if isinstance(dest_ptr, UnknownValue):
                # Split into null/non-null pointers:
                self.raise_split_value(dest_ptr)
            check_isinstance(dest_ptr, PointerToRegion)
            dest_region = dest_ptr.region
            log('dest_region: %r', dest_region)
            return dest_region
        raise NotImplementedError('eval_lvalue: %r %s' % (expr, expr))

    def eval_rvalue(self, expr, loc):
        """
        Return the value for the given expression, as an AbstractValue
        FIXME: also as a Region?
        """
        log('eval_rvalue: %r %s', expr, expr)
        if loc:
            check_isinstance(loc, gcc.Location)

        if isinstance(expr, AbstractValue):
            return expr
        if isinstance(expr, Region):
            return expr
        if isinstance(expr, gcc.IntegerCst):
            return ConcreteValue(expr.type, loc, expr.constant)
        if isinstance(expr, (gcc.VarDecl, gcc.ParmDecl)):
            region = self.var_region(expr)
            check_isinstance(region, Region)
            value = self.get_store(region, expr.type, loc)
            check_isinstance(value, AbstractValue)
            return value
            #return UnknownValue(expr.type, str(expr))
        if isinstance(expr, gcc.ComponentRef):
            #check_isinstance(expr.field, gcc.FieldDecl)
            region = self.get_field_region(expr, loc)#.target, expr.field.name)
            check_isinstance(region, Region)
            log('got field region for %s: %r', expr, region)
            try:
                value = self.get_store(region, expr.type, loc)
                log('got value: %r', value)
            except MissingValue:
                value = UnknownValue(expr.type, loc)
                log('no value; using: %r', value)
            check_isinstance(value, AbstractValue)
            return value
        if isinstance(expr, gcc.AddrExpr):
            log('expr.operand: %r', expr.operand)
            lvalue = self.eval_lvalue(expr.operand, loc)
            check_isinstance(lvalue, Region)
            return PointerToRegion(expr.type, loc, lvalue)
        if isinstance(expr, gcc.ArrayRef):
            log('expr.array: %r', expr.array)
            log('expr.index: %r', expr.index)
            lvalue = self.eval_lvalue(expr, loc)
            check_isinstance(lvalue, Region)
            rvalue = self.get_store(lvalue, expr.type, loc)
            check_isinstance(rvalue, AbstractValue)
            return rvalue
        if isinstance(expr, gcc.MemRef):
            log('expr.operand: %r', expr.operand)
            opvalue = self.eval_rvalue(expr.operand, loc)
            check_isinstance(opvalue, AbstractValue)
            log('opvalue: %r', opvalue)
            self.raise_any_null_ptr_deref(expr, opvalue)
            if isinstance(opvalue, UnknownValue):
                # Split into null/non-null pointers:
                self.raise_split_value(opvalue)
            check_isinstance(opvalue, PointerToRegion) # FIXME
            rvalue = self.get_store(opvalue.region, expr.type, loc)
            check_isinstance(rvalue, AbstractValue)
            return rvalue
        raise NotImplementedError('eval_rvalue: %r %s' % (expr, expr))
        return UnknownValue(expr.type, loc) # FIXME

    def assign(self, lhs, rhs, loc):
        log('assign(%r, %r)', lhs, rhs)
        log('assign(%s, %s)', lhs, rhs)
        if loc:
            check_isinstance(loc, gcc.Location)
        dest_region = self.eval_lvalue(lhs, loc)
        log('dest_region: %s %r', dest_region, dest_region)
        value = self.eval_rvalue(rhs, loc)
        log('value: %s %r', value, value)
        check_isinstance(value, AbstractValue)
        check_isinstance(dest_region, Region)
        self.value_for_region[dest_region] = value

    def var_region(self, var):
        check_isinstance(var, (gcc.VarDecl, gcc.ParmDecl))
        if var not in self.region_for_var:
            # Presumably a reference to a global variable:
            log('adding region for global var: %r', var)
            region = RegionForGlobal(var)
            # it is its own region:
            self.region_for_var[var] = region

            # Initialize the refcount of global PyObject instances
            # e.g. _Py_NoneStruct to 0 i.e. we don't own any references to them
            if str(var.type) == 'struct PyObject':
                from libcpychecker.refcounts import RefcountValue
                ob_refcnt = self.make_field_region(region, 'ob_refcnt') # FIXME: this should be a memref and fieldref
                self.value_for_region[ob_refcnt] = RefcountValue.borrowed_ref()
        return self.region_for_var[var]

    def element_region(self, ar, loc):
        log('element_region: %s', ar)
        check_isinstance(ar, gcc.ArrayRef)
        if loc:
            check_isinstance(loc, gcc.Location)

        log('  ar.array: %r', ar.array)
        log('  ar.index: %r', ar.index)
        parent = self.eval_lvalue(ar.array, loc)
        check_isinstance(parent, Region)
        log('  parent: %r', parent)
        index = self.eval_rvalue(ar.index, loc)
        check_isinstance(index, AbstractValue)
        log('  index: %r', index)
        if isinstance(index, ConcreteValue):
            index = index.value
        return self._array_region(parent, index)

    def pointer_plus_region(self, stmt):
        # Cope with treating pointers as arrays.
        # The constant appears to be in bytes, rather than as units of the type
        log('pointer_add_region')
        assert stmt.exprcode == gcc.PointerPlusExpr
        rhs = stmt.rhs
        a = self.eval_rvalue(rhs[0], stmt.loc)
        b = self.eval_rvalue(rhs[1], stmt.loc)
        log('a: %r', a)
        log('b: %r', b)
        if isinstance(a, PointerToRegion) and isinstance(b, ConcreteValue):
            parent = a.region
            log('%s', rhs[0].type)
            log('%s', rhs[0].type.dereference)
            sizeof = rhs[0].type.dereference.sizeof
            log('%s', sizeof)
            index = b.value / sizeof
            return self._array_region(parent, index)
        else:
            raise NotImplementedError("Don't know how to cope with pointer addition of\n  %r\nand\n  %rat %s"
                                      % (a, b, stmt.loc))

    def _array_region(self, parent, index):
        # Used by element_region, and pointer_add_region
        check_isinstance(parent, Region)
        check_isinstance(index, (int, long))
        if index in parent.fields:
            log('reusing')
            return parent.fields[index]
        log('not reusing')
        region = Region('%s[%s]' % (parent.name, index), parent)
        parent.fields[index] = region
        # it is its own region:
        self.region_for_var[region] = region
        return region

    def get_field_region(self, cr, loc): #target, field):
        check_isinstance(cr, gcc.ComponentRef)
        if loc:
            check_isinstance(loc, gcc.Location)
        #cr.debug()
        log('target: %r %s ', cr.target, cr.target)
        log('field: %r', cr.field)
        #fr = FIXME #self.make_field_region(target, field)
        if 1: # fr not in self.region_for_var:
            if 1: # cr.target not in self.region_for_var:
                log('foo')
                if isinstance(cr.target, gcc.MemRef):
                    ptr = self.eval_rvalue(cr.target.operand, loc) # FIXME
                    log('ptr: %r', ptr)
                    self.raise_any_null_ptr_deref(cr, ptr)
                    if isinstance(ptr, UnknownValue):
                        # It could be NULL; it could be non-NULL
                        # Split the analysis
                        # Non-NULL pointer:
                        log('splitting %s into non-NULL/NULL pointers', cr)
                        self.raise_split_value(ptr)
                    check_isinstance(ptr, PointerToRegion)
                    return self.make_field_region(ptr.region, cr.field.name)
                elif isinstance(cr.target, gcc.VarDecl):
                    log('bar')
                    vr = self.var_region(cr.target)
                    log('%s', vr)
                    return self.make_field_region(vr, cr.field.name)
                elif isinstance(cr.target, gcc.ComponentRef):
                    # nested field:
                    vr = self.get_field_region(cr.target, loc)
                    log('%s', vr)
                    return self.make_field_region(vr, cr.field.name)
        log('cr: %r %s', cr, cr)
        return self.region_for_var[cr]

    def string_constant_region(self, expr, loc):
        log('string_constant_region: %s', expr)
        check_isinstance(expr, gcc.StringCst)
        if loc:
            check_isinstance(loc, gcc.Location)
        region = RegionForStringConstant(expr.constant)
        return region

    def get_store(self, region, gcctype, loc):
        if gcctype:
            check_isinstance(gcctype, gcc.Type)
        if loc:
            check_isinstance(loc, gcc.Location)
        try:
            val = self._get_store_recursive(region, gcctype, loc)
            return val
        except MissingValue:
            # The first time we look up the value of a global, assign it a new
            # "unknown" value:
            if isinstance(region, RegionForGlobal):
                newval = UnknownValue(region.vardecl.type, region.vardecl.location)
                log('setting up %s for %s', newval, region.vardecl)
                self.value_for_region[region] = newval
                return newval

            # OK: no value known:
            return UnknownValue(gcctype, loc)

    def _get_store_recursive(self, region, gcctype, loc):
        check_isinstance(region, Region)
        # self.log(log)
        if region in self.value_for_region:
            return self.value_for_region[region]

        # Not found; try default value from parent region:
        if region.parent:
            try:
                return self._get_store_recursive(region.parent, gcctype, loc)
            except MissingValue:
                raise MissingValue(region)

        raise MissingValue(region)

    def make_heap_region(self, name, stmt):
        region = RegionOnHeap(name, stmt)
        # it is its own region:
        self.region_for_var[region] = region
        return region

    def make_field_region(self, target, field):
        check_isinstance(target, Region)
        check_isinstance(field, str)
        log('make_field_region(%r, %r)', target, field)
        if field in target.fields:
            log('reusing')
            return target.fields[field]
        log('not reusing')
        region = Region('%s.%s' % (target.name, field), target)
        target.fields[field] = region
        # it is its own region:
        self.region_for_var[region] = region
        return region


    def get_value_of_field_by_varname(self, varname, field):
        # Lookup varname.field
        # For use in writing selftests
        log('get_value_of_field_by_varname(%r, %r)', varname, field)
        check_isinstance(varname, str)
        check_isinstance(field, str)
        for k in self.region_for_var:
            if isinstance(k, gcc.VarDecl):
                if k.name == varname:
                    region = self.region_for_var[k]
                    region =  self.make_field_region(region, field)
                    value = self.value_for_region.get(region, None)
                    return value

    def get_value_of_field_by_region(self, region, field):
        # Lookup region->field
        # For use in writing selftests
        log('get_value_of_field_by_region(%r, %r)', region, field)
        check_isinstance(region, Region)
        check_isinstance(field, str)
        if field in region.fields:
            field_region = region.fields[field]
            return self.value_for_region.get(field_region, None)
        return None

    def init_for_function(self, fun):
        log('State.init_for_function(%r)', fun)
        self.fun = fun
        root_region = Region('root', None)
        stack = RegionOnStack('stack for %s' % fun.decl.name, root_region)

        nonnull_args = get_nonnull_arguments(fun.decl.type)
        for idx, parm in enumerate(fun.decl.arguments):
            region = RegionOnStack('region for %r' % parm, stack)
            self.region_for_var[parm] = region
            if idx in nonnull_args:
                # Make a non-NULL ptr:
                other = RegionOnStack('region-for-arg-%s' % parm, None)
                self.region_for_var[other] = other
                self.value_for_region[region] = PointerToRegion(parm.type, parm.location, other)
            else:
                self.value_for_region[region] = UnknownValue(parm.type, parm.location)
        for local in fun.local_decls:
            region = RegionOnStack('region for %r' % local, stack)
            self.region_for_var[local] = region
            self.value_for_region[region] = UninitializedData(local.type, fun.start)
        self.verify()

    def mktrans_assignment(self, lhs, rhs, desc):
        """
        Return a Transition to a state at the next location, with the RHS
        assigned to the LHS, if LHS is not None
        """
        log('mktrans_assignment(%r, %r, %r)', lhs, rhs, desc)
        if desc:
            check_isinstance(desc, str)
        new = self.copy()
        new.loc = self.loc.next_loc()
        if lhs:
            new.assign(lhs, rhs, self.loc.get_gcc_loc())
        return Transition(self, new, desc)

    def update_loc(self, newloc):
        new = self.copy()
        new.loc = newloc
        return new

    def use_next_loc(self):
        newloc = self.loc.next_loc()
        return self.update_loc(newloc)

    def get_gcc_loc_or_none(self):
        # Return the gcc.Location for this state, which could be None
        stmt = self.loc.get_stmt()
        if stmt:
            return stmt.loc

    def get_gcc_loc(self, fun):
        # Return a non-None gcc.Location for this state
        # Some statements have None for their location, but gcc.error() etc
        # don't allow this.  Use the end of the function for this case.
        stmt = self.loc.get_stmt()
        log('%s %r', stmt, stmt)
        if stmt:
            log('%s' % self.loc.get_stmt().loc)
            # grrr... not all statements have a non-NULL location
            gccloc = self.loc.get_stmt().loc
            if gccloc is None:
                gccloc = fun.end
            return gccloc
        else:
            return fun.end

    def raise_any_null_ptr_deref(self, expr, ptr):
        if isinstance(ptr, ConcreteValue):
            if ptr.is_null_ptr():
                # Read through NULL
                # If we earlier split the analysis into NULL/non-NULL
                # cases, then we're only considering the possibility
                # that this pointer was NULL; we don't know for sure
                # that it was.
                isdefinite = not hasattr(ptr, 'fromsplit')
                raise NullPtrDereference(self, expr, ptr, isdefinite)

    def raise_split_value(self, ptr_rvalue, loc=None):
        """
        Raise a SplitValue exception on the given rvalue, so that we can
        backtrack and split the current state into a version with an explicit
        NULL value and a version with a non-NULL value

        FIXME: we should split into multiple non-NULL values, covering the
        various aliasing possibilities
        """
        check_isinstance(ptr_rvalue, AbstractValue)
        check_isinstance(ptr_rvalue, UnknownValue)
        check_isinstance(ptr_rvalue.gcctype, gcc.PointerType)
        global region_id
        region = Region('heap-region-%i' % region_id, None)
        region_id += 1
        self.region_for_var[region] = region
        non_null_ptr = PointerToRegion(ptr_rvalue.gcctype, loc, region)
        null_ptr = ConcreteValue(ptr_rvalue.gcctype, loc, 0)
        raise SplitValue(ptr_rvalue, [non_null_ptr, null_ptr])

region_id = 0

class Transition:
    def __init__(self, src, dest, desc):
        check_isinstance(src, State)
        check_isinstance(dest, State)
        self.src = src
        self.dest = dest
        self.desc = desc

    def __repr__(self):
        return 'Transition(%r, %r)' % (self.dest, self.desc)

    def log(self, logger):
        logger('desc: %r' % self.desc)
        logger('dest:')
        self.dest.log(logger)

class Trace:
    """A sequence of States and Transitions"""
    def __init__(self):
        self.states = []
        self.transitions = []
        self.err = None

    def add(self, transition):
        check_isinstance(transition, Transition)
        self.states.append(transition.dest)
        self.transitions.append(transition)
        return self

    def add_error(self, err):
        self.err = err

    def copy(self):
        t = Trace()
        t.states = self.states[:]
        t.transitions = self.transitions[:]
        t.err = self.err # FIXME: should this be a copy?
        return t

    def log(self, logger, name):
        logger('%s:' % name)
        for i, state in enumerate(self.states):
            logger('%i:' % i)
            state.log(logger)
        if self.err:
            logger('  Trace ended with error: %s' % self.err)

    def get_last_stmt(self):
        return self.states[-1].loc.get_stmt()

    def return_value(self):
        return self.states[-1].return_rvalue

    def final_references(self):
        return self.states[-1].owned_refs

    def has_looped(self):
        """
        Is the tail state of the Trace at a location where it's been before?
        """
        endstate = self.states[-1]
        if hasattr(endstate, 'fromsplit'):
            # We have a state that was created from a SplitValue.  It will have
            # the same location as the state before it (before the split).
            # Don't treat it as a loop:
            return False
        if endstate.not_returning:
            # The handler not "exit" etc leads to a transition that has a
            # repeated location:
            return False
        for state in self.states[0:-1]:
            if state.loc == endstate.loc:
                return True

def true_edge(bb):
    for e in bb.succs:
        if e.true_value:
            return e

def false_edge(bb):
    for e in bb.succs:
        if e.false_value:
            return e

class Resources:
    # Resource tracking for a state
    def __init__(self):
        # Resources that we've acquired:
        self._acquisitions = []

        # Resources that we've released:
        self._releases = []

    def copy(self):
        new = Resources()
        new._acquisitions = self._acquisitions[:]
        new._releases = self._releases[:]
        return new

    def acquire(self, resource):
        self._acquisitions.append(resource)

    def release(self, resource):
        self._releases.append(resource)

    def log(self, logger):
        logger('resources:')
        logger('acquisitions: %s' % self._acquisitions)
        logger('releases: %s' % self._releases)

def iter_traces(fun, stateclass, prefix=None):
    """
    Traverse the tree of traces of program state, returning a list
    of Trace instances.

    For now, don't include any traces that contain loops, as a primitive
    way of ensuring termination of the analysis
    """
    log('iter_traces(%r, %r, %r)', fun, stateclass, prefix)
    if prefix is None:
        prefix = Trace()
        curstate = stateclass(Location.get_block_start(fun.cfg.entry),
                              None, None, None,
                              [],
                              Resources(),
                              ConcreteValue(get_PyObjectPtr(), fun.start, 0))
        curstate.init_for_function(fun)
    else:
        check_isinstance(prefix, Trace)
        curstate = prefix.states[-1]

        if curstate.has_returned:
            # This state has returned a value (and hence terminated):
            return [prefix]

        if curstate.not_returning:
            # This state has called "exit" or similar, and thus this
            # trace should terminate:
            return [prefix]

        # Stop interpreting when you see a loop, to ensure termination:
        if prefix.has_looped():
            log('loop detected; stopping iteration')
            # Don't return the prefix so far: it is not a complete trace
            return []

    # We need the prevstate to handle Phi nodes
    if len(prefix.states) > 1:
        prevstate = prefix.states[-2]
    else:
        prevstate = None

    prefix.log(log, 'PREFIX')
    log('  %s:%s', fun.decl.name, curstate.loc)
    try:
        transitions = curstate.get_transitions()
        check_isinstance(transitions, list)
    except PredictedError:
        # We're at a terminating state:
        err = sys.exc_info()[1]
        err.loc = prefix.get_last_stmt().loc
        trace_with_err = prefix.copy()
        trace_with_err.add_error(err)
        trace_with_err.log(log, 'FINISHED TRACE WITH ERROR: %s' % err)
        return [trace_with_err]
    except SplitValue:
        # Split the state up, splitting into parallel worlds with different
        # values for the given value
        # FIXME: this doesn't work; it thinks it's a loop :(
        err = sys.exc_info()[1]
        transitions = err.split(curstate)
        check_isinstance(transitions, list)

    log('transitions: %s', transitions)

    if len(transitions) > 0:
        result = []
        for transition in transitions:
            check_isinstance(transition, Transition)
            transition.dest.verify()

            # Recurse:
            for trace in iter_traces(fun, stateclass, prefix.copy().add(transition)):
                result.append(trace)
        return result
    else:
        # We're at a terminating state:
        prefix.log(log, 'FINISHED TRACE')
        return [prefix]

class StateGraph:
    """
    A graph of states, representing the various routes through a function,
    tracking state.

    For now, we give up when we encounter a loop, as an easy way to ensure
    termination of the analysis
    """
    def __init__(self, fun, logger, stateclass):
        check_isinstance(fun, gcc.Function)
        self.fun = fun
        self.states = []
        self.transitions = []
        self.stateclass = stateclass

        logger('StateGraph.__init__(%r)' % fun)

        # Recursively gather states:
        initial = stateclass(Location.get_block_start(fun.cfg.entry),
                             None, None, None,
                             [],
                             Resources(),
                             ConcreteValue(get_PyObjectPtr(), fun.start, 0))
        initial.init_for_function(fun)
        self.states.append(initial)
        self._gather_states(initial, logger)

    def _gather_states(self, curstate, logger):
        logger('  %s:%s' % (self.fun.decl.name, curstate.loc))
        try:
            transitions = curstate.get_transitions()
            #print transitions
            check_isinstance(transitions, list)
        except PredictedError:
            # We're at a terminating state:
            err = sys.exc_info()[1]
            errstate = curstate.copy()
            transition = Transition(curstate, errstate, str(err))
            self.states.append(transition.dest)
            self.transitions.append(transition)
            return

        logger('transitions:', 2)
        for t in transitions:
            t.log(logger, 3)

        if len(transitions) > 0:
            for transition in transitions:
                # FIXME: what about loops???
                check_isinstance(transition, Transition)
                self.states.append(transition.dest)
                self.transitions.append(transition)

                if transition.dest.has_returned():
                    # This state has returned a value (and hence terminated)
                    continue

                if transition.dest.not_returning():
                    # This state has called "exit" or similar, and thus this
                    # trace should terminate:
                    continue

                # Recurse:
                self._gather_states(transition.dest, logger)
        else:
            # We're at a terminating state:
            logger('FINISHED TRACE')

    def get_prev_state(self, state):
        assert state in self.states
        for t in self.transitions:
            if t.dest == state:
                return t.src
        # Not found:
        return None

def extra_text(msg, indent):
    sys.stderr.write('%s%s\n' % ('  ' * indent, msg))

