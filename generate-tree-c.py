#   Copyright 2011-2013, 2015 David Malcolm <dmalcolm@redhat.com>
#   Copyright 2011-2013, 2015 Red Hat, Inc.
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

from maketreetypes import iter_tree_types

from cpybuilder import (
    CompilationUnit,
    PyGetSetDefTable,
    PyMethodTable,
    PyGetSetDef,
    PyNumberMethods,
)
from wrapperbuilder import PyGccWrapperTypeObject
from testcpychecker import get_gcc_version

tree_types = list(iter_tree_types())
# FIXME: truncate the list, for ease of development:
# tree_types = list(iter_tree_types())[:3]

cu = CompilationUnit()
cu.add_include("gcc-python.h")
cu.add_include("gcc-python-wrappers.h")
cu.add_include("gcc-plugin.h")
cu.add_include("tree.h")
cu.add_include("function.h")
cu.add_include("basic-block.h")
cu.add_include("cp/cp-tree.h")
cu.add_include("c-family/c-common.h")
cu.add_include("gcc-c-api/gcc-tree.h")
cu.add_include("gcc-c-api/gcc-constant.h")
cu.add_include("gcc-c-api/gcc-declaration.h")
cu.add_include("gcc-c-api/gcc-type.h")
cu.add_include("cp/name-lookup.h")
cu.add_include("autogenerated-casts.h")

GCC_VERSION = get_gcc_version()
if GCC_VERSION >= 4009:
    # GCC 4.9 moved debug_tree here:
    cu.add_include("print-tree.h")

modinit_preinit = ""
modinit_postinit = ""


def generate_tree():
    #
    # Generate the gcc.Tree class:
    #
    global modinit_preinit
    global modinit_postinit

    cu.add_defn("""
static PyObject *
PyGccTree_get_type(struct PyGccTree *self, void *closure ATTRIBUTE_UNUSED)
{
    return PyGccTree_New(gcc_private_make_tree(TREE_TYPE(self->t.inner)));
}

static PyObject *
PyGccTree_get_addr(struct PyGccTree *self, void *closure ATTRIBUTE_UNUSED)
{
    return PyLong_FromVoidPtr(self->t.inner);
}

""")

    getsettable = PyGetSetDefTable(
        "PyGccTree_getset_table",
        [
            PyGetSetDef(
                "type",
                "PyGccTree_get_type",
                None,
                "Instance of gcc.Tree giving the type of the node",
            ),
            PyGetSetDef(
                "addr",
                "PyGccTree_get_addr",
                None,
                "The address of the underlying GCC object in memory",
            ),
            PyGetSetDef(
                "str_no_uid",
                "PyGccTree_get_str_no_uid",
                None,
                "A string representation of this object, like str(), but without including any internal UID",
            ),
        ],
        identifier_prefix="PyGccTree",
        typename="PyGccTree",
    )

    cu.add_defn(getsettable.c_defn())

    pytype = PyGccWrapperTypeObject(
        identifier="PyGccTree_TypeObj",
        localname="Tree",
        tp_name="gcc.Tree",
        tp_dealloc="PyGccWrapper_Dealloc",
        struct_name="PyGccTree",
        tp_new="PyType_GenericNew",
        tp_getset="PyGccTree_getset_table",
        tp_hash="(hashfunc)PyGccTree_hash",
        tp_str="(reprfunc)PyGccTree_str",
        tp_richcompare="PyGccTree_richcompare",
    )
    methods = PyMethodTable("PyGccTree_methods", [])
    methods.add_method(
        "debug", "PyGccTree_debug", "METH_VARARGS", "Dump the tree to stderr"
    )
    cu.add_defn("""
PyObject*
PyGccTree_debug(PyObject *self, PyObject *args ATTRIBUTE_UNUSED)
{
    PyGccTree *tree_obj;
    /* FIXME: type checking */
    tree_obj = (PyGccTree *)self;
    debug_tree(tree_obj->t.inner);
    Py_RETURN_NONE;
}
""")
    cu.add_defn(methods.c_defn())
    pytype.tp_methods = methods.identifier

    cu.add_defn(pytype.c_defn())
    modinit_preinit += pytype.c_invoke_type_ready()
    modinit_postinit += pytype.c_invoke_add_to_module()


generate_tree()

type_for_code_class = {
    "tcc_exceptional": "PyGccTree_TypeObj",
    "tcc_constant": "PyGccConstant_TypeObj",
    "tcc_type": "PyGccType_TypeObj",
    "tcc_declaration": "PyGccDeclaration_TypeObj",
    "tcc_reference": "PyGccReference_TypeObj",
    "tcc_comparison": "PyGccComparison_TypeObj",
    "tcc_unary": "PyGccUnary_TypeObj",
    "tcc_binary": "PyGccBinary_TypeObj",
    "tcc_statement": "PyGccStatement_TypeObj",
    "tcc_vl_exp": "PyGccVlExp_TypeObj",
    "tcc_expression": "PyGccExpression_TypeObj",
}


def generate_intermediate_tree_classes():
    # Generate a "middle layer" of gcc.Tree subclasses, corresponding to most of the
    # values of
    #    enum_tree_code_class
    # from GCC's tree.h
    global modinit_preinit
    global modinit_postinit

    for code_type in type_for_code_class.values():
        # We've already built the base class:
        if code_type == "PyGccTree_TypeObj":
            continue

        # Strip off the "PyGcc" prefix and "_TypeObj" suffix:
        localname = code_type[5:-8]

        getsettable = PyGetSetDefTable("gcc_%s_getset_table" % localname, [])

        methods = PyMethodTable("gcc_%s_methods" % localname, [])

        pytype = PyGccWrapperTypeObject(
            identifier=code_type,
            localname=localname,
            tp_name="gcc.%s" % localname,
            struct_name="PyGccTree",
            tp_new="PyType_GenericNew",
            tp_base="&PyGccTree_TypeObj",
            tp_getset=getsettable.identifier,
            tp_methods=methods.identifier,
        )

        def add_simple_getter(name, c_expression, doc):
            getsettable.add_gsdef(
                name,
                cu.add_simple_getter(
                    "PyGcc%s_get_%s" % (localname, name), "PyGccTree", c_expression
                ),
                None,
                doc,
            )

        if localname == "Declaration":
            cu.add_defn("""
PyObject *
PyGccDeclaration_get_name(struct PyGccTree *self, void *closure ATTRIBUTE_UNUSED)
{
    if (DECL_NAME(self->t.inner)) {
        return PyGccString_FromString(IDENTIFIER_POINTER (DECL_NAME (self->t.inner)));
    }
    Py_RETURN_NONE;
}

static PyObject *
PyGccDeclaration_get_location(struct PyGccTree *self, void *closure ATTRIBUTE_UNUSED)
{
    return PyGccLocation_New(gcc_decl_get_location(PyGccTree_as_gcc_decl(self)));
}
""")

            getsettable.add_gsdef(
                "name",
                "PyGccDeclaration_get_name",
                None,
                "The name of this declaration (string)",
            )
            getsettable.add_gsdef(
                "location",
                "PyGccDeclaration_get_location",
                None,
                "The gcc.Location for this declaration",
            )
            add_simple_getter(
                "is_artificial",
                "PyBool_FromLong(gcc_decl_is_artificial(PyGccTree_as_gcc_decl(self)))",
                "Is this a compiler-generated entity?",
            )
            add_simple_getter(
                "is_builtin",
                "PyBool_FromLong(gcc_decl_is_builtin(PyGccTree_as_gcc_decl(self)))",
                "Is this declaration built in by the compiler?",
            )
            pytype.tp_repr = "(reprfunc)PyGccDeclaration_repr"

        if localname == "Type":
            add_simple_getter(
                "name",
                "PyGccTree_New(gcc_type_get_name(PyGccTree_as_gcc_type(self)))",
                "The name of the type as a gcc.Tree, or None",
            )
            add_simple_getter(
                "pointer",
                "PyGccPointerType_New(gcc_type_get_pointer(PyGccTree_as_gcc_type(self)))",
                "The gcc.PointerType representing '(this_type *)'",
            )
            getsettable.add_gsdef(
                "attributes",
                "PyGccType_get_attributes",
                None,
                "The user-defined attributes on this type",
            )
            getsettable.add_gsdef(
                "sizeof",
                "PyGccType_get_sizeof",
                None,
                "sizeof() this type, as a gcc.IntegerCst",
            )

            def add_type(c_expr_for_node, typename):
                # Expose the given global type node within the gcc.Tree API
                #
                # The table is populated by tree.c:build_common_builtin_nodes
                # but unfortunately this seems to be called after our plugin is
                # initialized.
                #
                # Hence we add them as properties, so that they can be looked up on
                # demand, rather than trying to look them up once when the module
                # is set up
                cu.add_defn(
                    """
PyObject*
%s(PyObject *cls ATTRIBUTE_UNUSED, PyObject *args ATTRIBUTE_UNUSED)
{
    return PyGccTree_New(gcc_private_make_tree(%s));
}
"""
                    % ("PyGccType_get_%s" % typename, c_expr_for_node)
                )
                if typename == "size_t":
                    desc = typename
                else:
                    desc = typename.replace("_", " ")
                methods.add_method(
                    "%s" % typename,
                    "PyGccType_get_%s" % typename,
                    "METH_CLASS|METH_NOARGS",
                    "The builtin type '%s' as a gcc.Type (or None at startup before any compilation passes)"
                    % desc,
                )

            # Add the standard C integer types as properties.
            #
            # Tree nodes for the standard C integer types are defined in tree.h by
            #    extern GTY(()) tree integer_types[itk_none];
            # with macros to look into it of this form:
            #       #define unsigned_type_node    integer_types[itk_unsigned_int]
            #
            std_types = [
                "itk_char",
                "itk_signed_char",
                "itk_unsigned_char",
                "itk_short",
                "itk_unsigned_short",
                "itk_int",
                "itk_unsigned_int",
                "itk_long",
                "itk_unsigned_long",
                "itk_long_long",
                "itk_unsigned_long_long",
            ]
            if GCC_VERSION < 5000:
                # int128 seems to have gone away in
                # 9f75f0266e3611513f196c898088e2712a71eaf4, discussed at
                # https://gcc.gnu.org/ml/gcc-patches/2014-08/msg01396.html
                std_types += ["itk_int128", "itk_unsigned_int128"]
            for std_type in std_types:
                # strip off the "itk_" prefix
                assert std_type.startswith("itk_")
                stddef = std_type[4:]
                # add_simple_getter(stddef,
                #                  'PyGccTree_New(gcc_private_make_tree(integer_types[%s]))' % std_type,
                #                  "The builtin type '%s' as a gcc.Type (or None at startup before any compilation passes)" % stddef.replace('_', ' '))
                add_type("integer_types[%s]" % std_type, stddef)

            # Similarly,
            #   extern GTY(()) tree global_trees[TI_MAX];
            # holds various nodes, including many with a _TYPE suffix.
            # Here are some of them:
            for ti in (
                "TI_UINT32_TYPE",
                "TI_UINT64_TYPE",
                "TI_FLOAT_TYPE",
                "TI_DOUBLE_TYPE",
                "TI_LONG_DOUBLE_TYPE",
                "TI_VOID_TYPE",
                "TI_SIZE_TYPE",
            ):
                # strip off the "TI_" prefix and "_TYPE" suffix:
                assert ti.startswith("TI_")
                assert ti.endswith("_TYPE")

                if ti == "TI_SIZE_TYPE":
                    name = "size_t"
                else:
                    name = ti[3:-5].lower()
                add_type("global_trees[%s]" % ti, name)

        if localname == "Unary":
            add_simple_getter(
                "operand",
                "PyGccTree_New(gcc_unary_get_operand(PyGccTree_as_gcc_unary(self)))",
                "The operand of this expression, as a gcc.Tree",
            )

        # Corresponds to this gcc/tree.h macro:
        #   #define IS_EXPR_CODE_CLASS(CLASS)\
        #       ((CLASS) >= tcc_reference && (CLASS) <= tcc_expression)
        if localname in (
            "Reference",
            "Comparison",
            "Unary",
            "Binary",
            "Statement" "VlExp",
            "Expression",
        ):
            add_simple_getter(
                "location",
                (
                    "PyGccLocation_New(gcc_%s_get_location(PyGccTree_as_gcc_%s(self)))"
                    % (localname.lower(), localname.lower())
                ),
                "The source location of this expression",
            )

            methods.add_method(
                "get_symbol",
                "PyGccTree_get_symbol",  # they all share the implementation
                "METH_CLASS|METH_NOARGS",
                "FIXME",
            )

        cu.add_defn(methods.c_defn())
        cu.add_defn(getsettable.c_defn())
        cu.add_defn(pytype.c_defn())
        modinit_preinit += pytype.c_invoke_type_ready()
        modinit_postinit += pytype.c_invoke_add_to_module()


generate_intermediate_tree_classes()


def generate_tree_code_classes():
    # Generate all of the concrete gcc.Tree subclasses based on the:
    #    enum tree_code
    # as subclasses of the above layer:
    global modinit_preinit
    global modinit_postinit

    for tree_type in tree_types:
        base_type = type_for_code_class[tree_type.TYPE]

        cc = tree_type.camel_cased_string()

        getsettable = PyGetSetDefTable(
            "gcc_%s_getset_table" % cc,
            [],
            identifier_prefix="gcc_%s" % cc,
            typename="PyGccTree",
        )

        tp_as_number = None
        tp_repr = None
        tp_str = None

        methods = PyMethodTable("gcc_%s_methods" % cc, [])

        def get_getter_identifier(name):
            return "PyGcc%s_get_%s" % (cc, name)

        def add_simple_getter(name, c_expression, doc):
            getsettable.add_gsdef(
                name,
                cu.add_simple_getter(
                    get_getter_identifier(name), "PyGccTree", c_expression
                ),
                None,
                doc,
            )

        def add_complex_getter(name, doc):
            getsettable.add_gsdef(name, get_getter_identifier(name), None, doc)

        if cc == "AddrExpr":
            add_simple_getter(
                "operand",
                "PyGccTree_New(gcc_addr_expr_get_operand(PyGccTree_as_gcc_addr_expr(self)))",
                "The operand of this expression, as a gcc.Tree",
            )

        if cc == "StringCst":
            add_simple_getter(
                "constant",
                "PyGccString_FromString(gcc_string_constant_get_char_ptr(PyGccTree_as_gcc_string_constant(self)))",
                "The actual value of this constant, as a str",
            )
            tp_repr = "(reprfunc)PyGccStringConstant_repr"

        if cc == "IntegerCst":
            getsettable.add_gsdef(
                "constant",
                "PyGccIntegerConstant_get_constant",
                None,
                "The actual value of this constant, as an int/long",
            )
            number_methods = PyNumberMethods("PyGccIntegerConstant_number_methods")
            tp_as_number = number_methods.identifier
            number_methods.nb_int = "PyGccIntegerConstant_get_constant"
            cu.add_defn(number_methods.c_defn())
            tp_repr = "(reprfunc)PyGccIntegerConstant_repr"

        if cc == "RealCst":
            getsettable.add_gsdef(
                "constant",
                "PyGccRealCst_get_constant",
                None,
                "The actual value of this constant, as a float",
            )
            tp_repr = "(reprfunc)PyGccRealCst_repr"

        # TYPE_QUALS for various foo_TYPE classes:
        if tree_type.SYM in (
            "VOID_TYPE",
            "INTEGER_TYPE",
            "REAL_TYPE",
            "FIXED_POINT_TYPE",
            "COMPLEX_TYPE",
            "VECTOR_TYPE",
            "ENUMERAL_TYPE",
            "BOOLEAN_TYPE",
        ):
            for qual in ("const", "volatile", "restrict"):
                add_simple_getter(
                    qual,
                    "PyBool_FromLong(TYPE_QUALS(self->t.inner) & TYPE_QUAL_%s)"
                    % qual.upper(),
                    "Boolean: does this type have the '%s' modifier?" % qual,
                )
                add_simple_getter(
                    "%s_equivalent" % qual,
                    "PyGccTree_New(gcc_private_make_tree(build_qualified_type(self->t.inner, TYPE_QUALS(self->t.inner) | TYPE_QUAL_%s)))"
                    % qual.upper(),
                    "The gcc.Type for the %s version of this type" % qual,
                )
            add_simple_getter(
                "unqualified_equivalent",
                "PyGccTree_New(gcc_private_make_tree(build_qualified_type(self->t.inner, 0)))",
                "The gcc.Type for the unqualified version of this type",
            )
        if tree_type.SYM == "RECORD_TYPE":
            add_simple_getter(
                "const",
                "PyBool_FromLong(TYPE_READONLY(self->t.inner))",
                "Boolean: does this type have the 'const' modifier?",
            )

        if tree_type.SYM == "INTEGER_TYPE":
            add_simple_getter(
                "unsigned",
                "PyBool_FromLong(gcc_integer_type_is_unsigned(PyGccTree_as_gcc_integer_type(self)))",
                "Boolean: True for 'unsigned', False for 'signed'",
            )
            add_complex_getter(
                "signed_equivalent",
                "The gcc.IntegerType for the signed version of this type",
            )
            add_complex_getter(
                "unsigned_equivalent",
                "The gcc.IntegerType for the unsigned version of this type",
            )
            add_simple_getter(
                "max_value",
                "PyGccTree_New(gcc_integer_constant_as_gcc_tree(gcc_integer_type_get_max_value(PyGccTree_as_gcc_integer_type(self))))",
                "The maximum possible value for this type, as a gcc.IntegerCst",
            )
            add_simple_getter(
                "min_value",
                "PyGccTree_New(gcc_integer_constant_as_gcc_tree(gcc_integer_type_get_min_value(PyGccTree_as_gcc_integer_type(self))))",
                "The minimum possible value for this type, as a gcc.IntegerCst",
            )
            tp_repr = "(reprfunc)PyGccIntegerType_repr"

        if tree_type.SYM in ("INTEGER_TYPE", "REAL_TYPE", "FIXED_POINT_TYPE"):
            prefix = "gcc_%s" % tree_type.SYM.lower()
            add_simple_getter(
                "precision",
                "PyGccInt_FromLong(%s_get_precision(PyGccTree_as_%s(self)))"
                % (prefix, prefix),
                "The precision of this type in bits, as an int (e.g. 32)",
            )

        if tree_type.SYM in ("POINTER_TYPE", "ARRAY_TYPE", "VECTOR_TYPE"):
            prefix = "gcc_%s" % tree_type.SYM.lower()
            add_simple_getter(
                "dereference",
                (
                    "PyGccTree_New(gcc_type_as_gcc_tree(%s_get_dereference(PyGccTree_as_%s(self))))"
                    % (prefix, prefix)
                ),
                "The gcc.Type that this type points to'",
            )

        if tree_type.SYM == "POINTER_TYPE":
            tp_repr = "(reprfunc)PyGccPointerType_repr"

        if tree_type.SYM == "ARRAY_TYPE":
            add_simple_getter(
                "range",
                "PyGccTree_New(gcc_private_make_tree(TYPE_DOMAIN(self->t.inner)))",
                "The gcc.Type that is the range of this array type",
            )

        if tree_type.SYM == "ARRAY_REF":
            add_simple_getter(
                "array",
                "PyGccTree_New(gcc_array_ref_get_array(PyGccTree_as_gcc_array_ref(self)))",
                "The gcc.Tree for the array being referenced'",
            )
            add_simple_getter(
                "index",
                "PyGccTree_New(gcc_array_ref_get_index(PyGccTree_as_gcc_array_ref(self)))",
                "The gcc.Tree for index being referenced'",
            )
            tp_repr = "(reprfunc)PyGccArrayRef_repr"

        if tree_type.SYM == "COMPONENT_REF":
            add_simple_getter(
                "target",
                "PyGccTree_New(gcc_component_ref_get_target(PyGccTree_as_gcc_component_ref(self)))",
                "The gcc.Tree that for the container of the field'",
            )
            add_simple_getter(
                "field",
                "PyGccTree_New(gcc_component_ref_get_field(PyGccTree_as_gcc_component_ref(self)))",
                "The gcc.FieldDecl for the field within the target'",
            )
            tp_repr = "(reprfunc)PyGccComponentRef_repr"

        if tree_type.SYM == "MEM_REF":
            add_simple_getter(
                "operand",
                "PyGccTree_New(gcc_mem_ref_get_operand(PyGccTree_as_gcc_mem_ref(self)))",
                "The gcc.Tree that for the pointer expression'",
            )

        if tree_type.SYM == "BIT_FIELD_REF":
            add_simple_getter(
                "operand",
                "PyGccTree_New(gcc_private_make_tree(TREE_OPERAND(self->t.inner, 0)))",
                "The gcc.Tree for the structure or union expression",
            )
            add_simple_getter(
                "num_bits",
                "PyGccTree_New(gcc_private_make_tree(TREE_OPERAND(self->t.inner, 1)))",
                "The number of bits being referenced, as a gcc.IntegerCst",
            )
            add_simple_getter(
                "position",
                "PyGccTree_New(gcc_private_make_tree(TREE_OPERAND(self->t.inner, 2)))",
                "The position of the first referenced bit, as a gcc.IntegerCst",
            )

        if tree_type.SYM in ("RECORD_TYPE", "UNION_TYPE", "QUAL_UNION_TYPE"):
            add_simple_getter(
                "fields", "PyGcc_GetFields(self)", "The fields of this type"
            )
            add_simple_getter(
                "methods", "PyGcc_GetMethods(self)", "The methods of this type"
            )

        if tree_type.SYM == "ENUMERAL_TYPE":
            add_simple_getter(
                "values",
                "PyGcc_TreeMakeListOfPairsFromTreeListChain(TYPE_VALUES(self->t.inner))",
                "The values of this type",
            )

        if tree_type.SYM == "IDENTIFIER_NODE":
            add_simple_getter(
                "name",
                "PyGccStringOrNone(IDENTIFIER_POINTER(self->t.inner))",
                "The name of this gcc.IdentifierNode, as a string",
            )
            tp_repr = "(reprfunc)PyGccIdentifierNode_repr"

        if tree_type.SYM == "VAR_DECL":
            add_simple_getter(
                "initial",
                "PyGccTree_New(gcc_constructor_as_gcc_tree(gcc_var_decl_get_initial(PyGccTree_as_gcc_var_decl(self))))",
                "The initial value for this variable as a gcc.Constructor, or None",
            )
            add_simple_getter(
                "static",
                "PyBool_FromLong(gcc_var_decl_is_static(PyGccTree_as_gcc_var_decl(self)))",
                "Boolean: is this variable to be allocated with static storage",
            )

        if tree_type.SYM == "CONSTRUCTOR":
            add_complex_getter(
                "elements",
                "The elements of this constructor, as a list of (index, gcc.Tree) pairs",
            )

        if tree_type.SYM == "TRANSLATION_UNIT_DECL":
            add_simple_getter(
                "block",
                "PyGccBlock_New(gcc_translation_unit_decl_get_block(PyGccTree_as_gcc_translation_unit_decl(self)))",
                "The gcc.Block for this namespace",
            )
            add_simple_getter(
                "language",
                "PyGccString_FromString(gcc_translation_unit_decl_get_language(PyGccTree_as_gcc_translation_unit_decl(self)))",
                "The source language of this translation unit, as a string",
            )

        if tree_type.SYM == "BLOCK":
            add_simple_getter(
                "vars",
                "PyGcc_TreeListFromChain(BLOCK_VARS(self->t.inner))",
                "The list of gcc.Tree for the declarations and labels in this block",
            )

        if tree_type.SYM == "NAMESPACE_DECL":
            add_simple_getter(
                "alias_of",
                "PyGccTree_New(gcc_private_make_tree(DECL_NAMESPACE_ALIAS(self->t.inner)))",
                "None if not an alias, otherwise the gcc.NamespaceDecl we alias",
            )
            add_simple_getter(
                "declarations",
                "PyGccNamespaceDecl_declarations(self->t.inner)",
                "The list of gcc.Declarations within this namespace",
            )
            add_simple_getter(
                "namespaces",
                "PyGccNamespaceDecl_namespaces(self->t.inner)",
                "The list of gcc.NamespaceDecl objects and gcc.TypeDecl of Unions nested in this namespace",
            )
            methods.add_method(
                "lookup",
                "(PyCFunction)PyGccNamespaceDecl_lookup",
                "METH_VARARGS|METH_KEYWORDS",
                "Look up the given string within this namespace",
            )
            methods.add_method(
                "unalias",
                "(PyCFunction)PyGccNamespaceDecl_unalias",
                "METH_VARARGS|METH_KEYWORDS",
                "A gcc.NamespaceDecl of this namespace that is not an alias",
            )

        if tree_type.SYM == "TYPE_DECL":
            getsettable.add_gsdef(
                "pointer",
                "PyGccTypeDecl_get_pointer",
                None,
                "The gcc.PointerType representing '(this_type *)'",
            )
            getsettable.add_gsdef(
                "original_type",
                "PyGccTypeDecl_get_original_type",
                None,
                "The gcc.Type from which this type was typedef'd from.'",
            )

        if tree_type.SYM == "FUNCTION_TYPE":
            getsettable.add_gsdef(
                "argument_types",
                "PyGccFunction_TypeObj_get_argument_types",
                None,
                "A tuple of gcc.Type instances, representing the argument types of this function type",
            )
            getsettable.add_gsdef(
                "is_variadic",
                "PyGccFunction_TypeObj_is_variadic",
                None,
                "Boolean: is this function variadic",
            )

        if tree_type.SYM == "METHOD_TYPE":
            getsettable.add_gsdef(
                "argument_types",
                "PyGccMethodType_get_argument_types",
                None,
                "A tuple of gcc.Type instances, representing the argument types of this method type",
            )
            getsettable.add_gsdef(
                "is_variadic",
                "PyGccMethodType_is_variadic",
                None,
                "Boolean: is this method variadic",
            )

        if tree_type.SYM == "FUNCTION_DECL":
            getsettable.add_gsdef(
                "fullname",
                "PyGccFunctionDecl_get_fullname",
                None,
                "C++ only: the full name of this function declaration",
            )
            add_simple_getter(
                "function",
                "PyGccFunction_New(gcc_private_make_function(DECL_STRUCT_FUNCTION(self->t.inner)))",
                "The gcc.Function (or None) for this declaration",
            )
            add_simple_getter(
                "arguments",
                "PyGcc_TreeListFromChain(DECL_ARGUMENTS(self->t.inner))",
                "List of gcc.ParmDecl",
            )
            add_simple_getter(
                "result",
                "PyGccTree_New(gcc_private_make_tree(DECL_RESULT_FLD(self->t.inner)))",
                "The gcc.ResultDecl for the return value",
            )
            getsettable.add_gsdef(
                "callgraph_node",
                "PyGccFunctionDecl_get_callgraph_node",
                None,
                "The gcc.CallgraphNode for this function declaration, or None",
            )

            for attr in ("public", "private", "protected", "static"):
                getsettable.add_simple_getter(
                    cu,
                    "is_%s" % attr,
                    "PyBool_FromLong(TREE_%s(self->t.inner))" % attr.upper(),
                    None,
                )

        if tree_type.SYM == "SSA_NAME":
            # c.f. "struct GTY(()) tree_ssa_name":
            add_simple_getter(
                "var",
                "PyGccTree_New(gcc_ssa_name_get_var(PyGccTree_as_gcc_ssa_name(self)))",
                "The variable being referenced'",
            )
            add_simple_getter(
                "def_stmt",
                "PyGccGimple_New(gcc_ssa_name_get_def_stmt(PyGccTree_as_gcc_ssa_name(self)))",
                "The gcc.Gimple statement which defines this SSA name'",
            )
            add_simple_getter(
                "version",
                "PyGccInt_FromLong(gcc_ssa_name_get_version(PyGccTree_as_gcc_ssa_name(self)))",
                "The SSA version number of this SSA name'",
            )
            tp_repr = "(reprfunc)PyGccSsaName_repr"

        if tree_type.SYM == "TREE_LIST":
            # c.f. "struct GTY(()) tree_list":
            tp_repr = "(reprfunc)PyGccTreeList_repr"

        if tree_type.SYM == "CASE_LABEL_EXPR":
            add_simple_getter(
                "low",
                "PyGccTree_New(gcc_case_label_expr_get_low(PyGccTree_as_gcc_case_label_expr(self)))",
                "The low value of the case label, as a gcc.Tree (or None for the default)",
            )
            add_simple_getter(
                "high",
                "PyGccTree_New(gcc_case_label_expr_get_high(PyGccTree_as_gcc_case_label_expr(self)))",
                "The high value of the case label, if any, as a gcc.Tree (None for the default and for single-valued case labels)",
            )
            add_simple_getter(
                "target",
                "PyGccTree_New(gcc_label_decl_as_gcc_tree(gcc_case_label_expr_get_target(PyGccTree_as_gcc_case_label_expr(self))))",
                "The target of the case label, as a gcc.LabelDecl",
            )
            tp_repr = "(reprfunc)PyGccCaseLabelExpr_repr"

        cu.add_defn(getsettable.c_defn())
        cu.add_defn(methods.c_defn())
        pytype = PyGccWrapperTypeObject(
            identifier="PyGcc%s_TypeObj" % cc,
            localname=cc,
            tp_name="gcc.%s" % cc,
            struct_name="PyGccTree",
            tp_new="PyType_GenericNew",
            tp_base="&%s" % base_type,
            tp_getset=getsettable.identifier,
            tp_str=tp_str,
            tp_repr=tp_repr,
            tp_methods=methods.identifier,
        )
        if tp_as_number:
            pytype.tp_as_number = "&%s" % tp_as_number
        cu.add_defn(pytype.c_defn())
        modinit_preinit += pytype.c_invoke_type_ready()
        modinit_postinit += pytype.c_invoke_add_to_module()

    cu.add_defn("\n/* Map from GCC tree codes to PyGccWrapperTypeObject* */\n")
    cu.add_defn("PyGccWrapperTypeObject *pytype_for_tree_code[] = {\n")
    for tree_type in tree_types:
        cu.add_defn(
            "    &PyGcc%s_TypeObj, /* %s */\n"
            % (tree_type.camel_cased_string(), tree_type.SYM)
        )
    cu.add_defn("};\n\n")

    cu.add_defn("\n/* Map from PyGccWrapperTypeObject* to GCC tree codes*/\n")
    cu.add_defn("int \n")
    cu.add_defn(
        "PyGcc_tree_type_object_as_tree_code(PyObject *cls, enum tree_code *out)\n"
    )
    cu.add_defn("{\n")
    for tree_type in tree_types:
        cu.add_defn(
            "    if (cls == (PyObject*)&PyGcc%s_TypeObj) {\n"
            "        *out = %s; return 0;\n"
            "    }\n" % (tree_type.camel_cased_string(), tree_type.SYM)
        )
    cu.add_defn("    return -1;\n")
    cu.add_defn("}\n")

    cu.add_defn("""
PyGccWrapperTypeObject*
PyGcc_autogenerated_tree_type_for_tree_code(enum tree_code code, int borrow_ref)
{
    PyGccWrapperTypeObject *result;

    assert(code >= 0);
    assert(code < MAX_TREE_CODES);

    result = pytype_for_tree_code[code];

    if (!borrow_ref) {
        Py_INCREF(result);
    }
    return result;
}

PyGccWrapperTypeObject*
PyGcc_autogenerated_tree_type_for_tree(gcc_tree t, int borrow_ref)
{
    enum tree_code code = TREE_CODE(t.inner);
    /* printf("code:%i\\n", code); */
    return PyGcc_autogenerated_tree_type_for_tree_code(code, borrow_ref);
}
""")


generate_tree_code_classes()

cu.add_defn(
    """
int autogenerated_tree_init_types(void)
{
"""
    + modinit_preinit
    + """
    return 1;

error:
    return 0;
}
"""
)

cu.add_defn(
    """
void autogenerated_tree_add_types(PyObject *m)
{
"""
    + modinit_postinit
    + """
}
"""
)


print(cu.as_str())
